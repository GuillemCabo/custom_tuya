"""Web interface for the Haier AC: live values, settings, Max heat / Max cold presets.

Run:  python -m acweb.server            (http://127.0.0.1:8320/)
Env:  AC_HOST (default 127.0.0.1), AC_PORT (8320), AC_IP (192.168.1.84),
      AC_ID (first device in devices.json if unset), AC_VERSION (3.5), AC_DEVICES (devices.json),
      AC_NAME (page title, "Air conditioner")
"""
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .device import AC, NOISY
from .dps import DPS, PRESETS, convert

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "static" / "index.html").read_bytes()
log = logging.getLogger("acweb")


def load_ac():
    path = Path(os.environ.get("AC_DEVICES", "devices.json"))
    devices = json.loads(path.read_text())
    want = os.environ.get("AC_ID")
    dev = next((d for d in devices if d["id"] == want), None) if want else \
        next((d for d in devices if d.get("category") == "kt"), devices[0])
    if not dev:
        raise SystemExit(f"device {want} not found in {path}")
    return AC(dev["id"], os.environ.get("AC_IP", "192.168.1.84"), dev["key"],
              float(os.environ.get("AC_VERSION", "3.5"))), os.environ.get("AC_NAME", "Air conditioner")


AC_DEV, AC_NAME = None, None


def snapshot():
    st = dict(AC_DEV.state)
    values = {}
    for dp, raw in st.items():
        shown, note = convert(dp, raw, st)
        values[dp] = dict(raw=raw, value=shown, note=note)
    return dict(name=AC_NAME, connected=AC_DEV.connected, last_update=AC_DEV.last_update,
                last_error=AC_DEV.last_error, values=values,
                events=[e for e in AC_DEV.events][:100])


def meta():
    return dict(dps=DPS, presets={k: dict(label=v["label"], steps=v["steps"]) for k, v in PRESETS.items()},
                noisy=sorted(NOISY))


class Handler(BaseHTTPRequestHandler):
    server_version = "acweb"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, INDEX, "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, snapshot())
        if self.path == "/api/meta":
            return self._send(200, meta())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/set":
                return self._send(200, {"results": [AC_DEV.set(str(body["dp"]), body["value"])]})
            if self.path == "/api/preset":
                return self._send(200, {"results": AC_DEV.preset(body["name"])})
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("command failed")
            self._send(502, {"error": str(e)})


def main():
    global AC_DEV, AC_NAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    AC_DEV, AC_NAME = load_ac()
    AC_DEV.start()
    host, port = os.environ.get("AC_HOST", "127.0.0.1"), int(os.environ.get("AC_PORT", "8320"))
    log.info("web interface on http://%s:%d/", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
