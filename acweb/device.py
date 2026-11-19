"""Keeps one local connection to the AC: tracks live state, logs changes, sends commands."""
import collections
import logging
import threading
import time
from datetime import datetime

import tinytuya

from .dps import DPS, PRESETS, validate

log = logging.getLogger("acweb")

HEARTBEAT_S = 10
FULL_REFRESH_S = 30
NOISY = {"3", "128", "18", "112"}   # sensors: logged, but kept out of "settings changed" lists


class AC:
    def __init__(self, dev_id, ip, key, version=3.5):
        self.dev_id, self.ip, self.key, self.version = dev_id, ip, key, version
        self.lock = threading.Lock()          # guards the socket
        self.state = {}
        self.connected = False
        self.last_update = None
        self.last_error = None
        self.events = collections.deque(maxlen=300)
        self._dev = None
        self._stop = threading.Event()

    # ---------- connection ----------
    def _new_device(self):
        d = tinytuya.Device(self.dev_id, self.ip, self.key, version=self.version, persist=True)
        d.set_socketTimeout(2)
        d.set_socketRetryLimit(1)
        return d

    def _merge(self, dps, source):
        now = datetime.now()
        for dp, val in (dps or {}).items():
            old = self.state.get(dp, None)
            if dp in self.state and old != val:
                self.events.appendleft(dict(time=now.isoformat(timespec="seconds"), dp=dp,
                                            label=DPS.get(dp, {}).get("label", f"DP {dp}"),
                                            old=old, new=val, source=source))
            self.state[dp] = val
        if dps:
            self.last_update = now.isoformat(timespec="seconds")

    def _status(self):
        r = self._dev.status()
        if not r or "Error" in r:
            raise ConnectionError((r or {}).get("Error", "no reply"))
        self._merge(r.get("dps"), "device")

    def run(self):
        last_hb = last_full = 0.0
        while not self._stop.is_set():
            try:
                with self.lock:
                    if self._dev is None:
                        self._dev = self._new_device()
                        self._status()
                        self.connected, self.last_error = True, None
                        last_hb = last_full = time.time()
                        log.info("connected to %s", self.ip)
                    if time.time() - last_full > FULL_REFRESH_S:
                        self._status()
                        last_full = time.time()
                    elif time.time() - last_hb > HEARTBEAT_S:
                        self._dev.heartbeat(nowait=True)
                        last_hb = time.time()
                    msg = self._dev.receive()
                if msg and "Error" in msg:
                    if msg.get("Err") not in ("902",):          # 902 = just a receive timeout
                        raise ConnectionError(msg.get("Error"))
                elif msg:
                    self._merge(msg.get("dps"), "device")
                time.sleep(0.05)                                # let command calls grab the lock
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                self.connected, self.last_error = False, str(e)
                log.warning("connection problem: %s", e)
                with self.lock:
                    try:
                        if self._dev:
                            self._dev.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._dev = None
                self._stop.wait(3)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="ac-link").start()

    # ---------- commands ----------
    def set(self, dp, value):
        value = validate(dp, value)
        with self.lock:
            if self._dev is None:
                raise ConnectionError("not connected to the AC")
            self._dev.set_value(int(dp), value, nowait=True)
            time.sleep(1.5)
            self._status()
        actual = self.state.get(dp)
        return dict(dp=dp, label=DPS[dp]["label"], sent=value, reported=actual, accepted=actual == value)

    def preset(self, name):
        p = PRESETS.get(name)
        if not p:
            raise ValueError(f"unknown preset {name}")
        results = []
        for dp, value in p["steps"]:
            try:
                results.append(self.set(dp, value))
            except Exception as e:  # noqa: BLE001 - report each step, keep going
                results.append(dict(dp=dp, label=DPS[dp]["label"], sent=value, reported=None,
                                    accepted=False, error=str(e)))
        return results
