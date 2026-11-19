"""Minimal client for Xiongmai's DVRIP/"Sofia" protocol (TCP 34567), used by iCSee/XMEye cameras.

Only what the camera panel needs: login, read/write named configs, system info, operations.
"""
import hashlib
import json
import socket
import struct
import threading
import time

HEADER = struct.Struct("<BB2xII2xHI")  # head 0xFF, version, session, sequence, msg id, data length
OK_CODES = {100, 515}                  # 515 = success, device will restart
ERRORS = {101: "unknown error", 102: "not supported", 103: "illegal request", 104: "user already logged in",
          105: "user not logged in", 106: "wrong user name or password", 107: "no permission", 108: "timeout",
          109: "search failed", 110: "user not found", 111: "user already exists", 112: "user is locked",
          119: "bad data", 150: "success, restart needed", 203: "wrong password", 205: "user locked"}

LOGIN, KEEPALIVE, SYSINFO, ABILITY = 1000, 1006, 1020, 1360
CONFIG_SET, CONFIG_GET, PTZ, OPERATION = 1040, 1042, 1400, 1450
RELOGIN = {105, 108}  # session expired / timed out: log in again and retry


def sofia_hash(password):
    digest = hashlib.md5(password.encode()).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(digest[2 * i] + digest[2 * i + 1]) % 62] for i in range(8))


class DVRIPError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class DVRIP:
    def __init__(self, host, user, password, port=34567, timeout=6):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout
        self.sock, self.session, self.seq = None, 0, 0
        self.lock = threading.Lock()

    # --- transport -------------------------------------------------------------------------
    def _connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        self.session, self.seq = 0, 0
        reply = self._exchange(LOGIN, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                                       "PassWord": sofia_hash(self.password), "UserName": self.user})
        self.session = int(reply["SessionID"], 16)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("camera closed the connection")
            buf += chunk
        return buf

    def _exchange(self, msg, data):
        body = json.dumps(data, separators=(",", ":")).encode() + b"\x0a\x00"
        self.sock.sendall(HEADER.pack(0xFF, 0, self.session, self.seq, msg, len(body)) + body)
        self.seq += 1
        _, _, _, _, _, length = HEADER.unpack(self._recv(HEADER.size))
        raw = self._recv(length).rstrip(b"\x00\x0a ")
        reply = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        ret = reply.get("Ret", 100)
        if ret not in OK_CODES and ret != 150:
            raise DVRIPError(f"{data.get('Name', msg)}: {ERRORS.get(ret, 'not supported by this camera')} "
                             f"(code {ret})", ret)
        return reply

    def request(self, msg, data):
        """Send one request, logging in (again) if needed."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.sock:
                        self._connect()
                    data = dict(data, SessionID=f"0x{self.session:08X}")
                    return self._exchange(msg, data)
                except DVRIPError as e:
                    if e.code not in RELOGIN or attempt == 2:
                        raise
                    self.close()
                except (OSError, ConnectionError, ValueError) as e:
                    self.close()
                    if attempt == 2:
                        raise DVRIPError(f"camera unreachable: {e}") from e

    # --- API -------------------------------------------------------------------------------
    def get(self, name):
        return self.request(CONFIG_GET, {"Name": name}).get(name)

    def set(self, name, value):
        reply = self.request(CONFIG_SET, {"Name": name, name: value})
        return reply.get("Ret", 100)

    def info(self, name="SystemInfo"):
        return self.request(SYSINFO, {"Name": name}).get(name)

    def ability(self, name="SystemFunction"):
        return self.request(ABILITY, {"Name": name}).get(name)

    def operation(self, name, value):
        return self.request(OPERATION, {"Name": name, name: value}).get("Ret", 100)

    def ptz(self, command, preset=65535, step=5, tour=0):
        """Pan/tilt/zoom. Movement runs until the same command is sent with preset=-1."""
        return self.request(PTZ, {"Name": "OPPTZControl", "OPPTZControl": {"Command": command, "Parameter": {
            "AUX": {"Number": 0, "Status": "On"}, "Channel": 0, "MenuOpts": "Enter",
            "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0}, "Pattern": "SetBegin",
            "Preset": preset, "Step": step, "Tour": tour}}}).get("Ret", 100)

    def files(self, begin, end, event="*", channel=0):
        """Recordings between two "YYYY-MM-DD HH:MM:SS" times (the camera returns at most 64 per
        query, so this pages through). event: * all, R regular, M motion, A alarm, H human."""
        out, seen = [], set()
        while True:
            reply = self.request(1440, {"Name": "OPFileQuery", "OPFileQuery": {
                "BeginTime": begin, "EndTime": end, "Channel": channel, "DriverTypeMask": "0x0000FFFF",
                "Event": event, "Type": "h264", "StreamType": "0x00000000"}})
            batch = [f for f in reply.get("OPFileQuery") or [] if f["FileName"] not in seen]
            if not batch:
                return out
            for f in batch:
                seen.add(f["FileName"])
                out.append(f)
            last = max(f["EndTime"] for f in batch)
            if len(reply.get("OPFileQuery") or []) < 64 or last >= end or last <= begin:
                return out
            begin = last

    def download(self, f, out, idle_timeout=15, stop=None, max_rate=None):
        """Download one recording (an entry from files()) to the file object `out`. The camera
        sends it on a second connection claimed with this login's session. Returns bytes written.
        max_rate (bytes/s) reads slowly so the camera sends slowly: full-speed reads of its SD card
        while it records can stall all of its services for minutes."""
        params = {"Action": "Claim", "StartTime": f["BeginTime"], "EndTime": f["EndTime"],
                  "Parameter": {"PlayMode": "ByName", "FileName": f["FileName"], "StreamType": 0,
                                "Value": 0, "TransMode": "TCP"}}
        with self.lock:
            if not self.sock:
                self._connect()
        data = socket.create_connection((self.host, self.port), self.timeout)
        data.settimeout(idle_timeout)
        sid = f"0x{self.session:08X}"

        def send(sock, seq, msg, body):
            raw = json.dumps(dict(body, SessionID=sid), separators=(",", ":")).encode() + b"\x0a\x00"
            sock.sendall(HEADER.pack(0xFF, 0, self.session, seq, msg, len(raw)) + raw)

        def recv(sock, n):
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
            return buf

        expected = int(f.get("FileLength", "0x0"), 16) * 1024   # FileLength is in KB
        written, started = 0, time.monotonic()
        try:
            send(data, 0, 1424, {"Name": "OPPlayBack", "OPPlayBack": params})          # claim
            _, _, _, _, msg, length = HEADER.unpack(recv(data, HEADER.size))
            reply = json.loads(recv(data, length).rstrip(b"\x00\x0a ") or b"{}")
            if reply.get("Ret", 100) not in OK_CODES:
                raise DVRIPError(f"download claim refused (code {reply.get('Ret')})", reply.get("Ret"))
            self.request(1420, {"Name": "OPPlayBack", "OPPlayBack": dict(params, Action="DownloadStart")})
            while True:
                try:
                    _, _, _, _, msg, length = HEADER.unpack(recv(data, HEADER.size))
                except (socket.timeout, ConnectionError):
                    break
                if length == 0:
                    break
                out.write(recv(data, length))
                written += length
                if max_rate:
                    ahead = written / max_rate - (time.monotonic() - started)
                    if ahead > 0:
                        time.sleep(ahead)
                if expected and written >= expected or stop is not None and stop.is_set():
                    break
        finally:
            try:
                self.request(1420, {"Name": "OPPlayBack", "OPPlayBack": dict(params, Action="DownloadStop")})
            except DVRIPError:
                pass
            data.close()
        return written

    def keepalive(self):
        return self.request(KEEPALIVE, {"Name": "KeepAlive"})
