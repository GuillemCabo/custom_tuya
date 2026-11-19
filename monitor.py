"""Live monitor for the Haier AC: prints every data-point change it pushes."""
import json, sys, time
from datetime import datetime
import tinytuya

AC_ID, AC_IP = "bff73e10b06a1f1061ipe2", "192.168.1.84"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 1800

ac = next(d for d in json.load(open("devices.json")) if d["id"] == AC_ID)
names = {k: v["code"] for k, v in (ac.get("mapping") or {}).items()}

def ts():
    return datetime.now().strftime("%H:%M:%S")

names.setdefault("128", "temp_outdoor")

# The AC's Wi-Fi module reports raw Haier sensor bytes, not the °C its spec claims:
#   room (dp 3)     = raw / 2   (half-degree steps)
#   outdoor (dp 128) = raw - 64
CONVERT = {"3": lambda r: r / 2, "128": lambda r: r - 64}

def label(dp):
    return f"{names.get(dp, 'dp' + dp)}({dp})"

def show(dp, val):
    if dp in CONVERT and isinstance(val, int):
        txt = f"{CONVERT[dp](val):g} °C [raw {val}]"
        if dp == "3" and state.get("1") is False:
            txt += " (unreliable while AC is off)"
        return txt
    return str(val)

d = tinytuya.Device(AC_ID, AC_IP, ac["key"], version=3.5, persist=True)
state = {}
end = time.time() + DURATION
last_hb = 0

def handle(msg):
    if not msg:
        return
    if "Error" in msg:
        print(ts(), "ERROR", msg.get("Error"), flush=True)
        return
    for dp, val in (msg.get("dps") or {}).items():
        old = state.get(dp, "?")
        if old != val:
            state[dp] = val
            if old != "?":
                print(f"{ts()}  {label(dp):24} {show(dp, old)} -> {show(dp, val)}", flush=True)

initial = d.status()
for dp, val in (initial.get("dps") or {}).items():
    state[dp] = val
print(ts(), "CONNECTED. Initial state:", flush=True)
for dp, val in state.items():
    if dp in names:
        print(f"           {label(dp):24} {show(dp, val)}", flush=True)
print(ts(), "Watching for changes...", flush=True)

while time.time() < end:
    if time.time() - last_hb > 10:
        d.heartbeat(nowait=True)
        last_hb = time.time()
    msg = d.receive()
    if msg and msg.get("Err") == "904":   # connection dropped, reconnect
        print(ts(), "reconnecting", flush=True)
        time.sleep(2)
        handle(d.status())
        continue
    handle(msg)
print(ts(), "DONE", flush=True)
