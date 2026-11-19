# custom_tuya

Local control and monitoring for a Haier Wi‑Fi air conditioner on the Tuya platform
(Tuya product `9xvzf8c0bg33eenj`, "海尔家用空调（全功能）", local protocol 3.5).
Everything talks to the AC directly on the LAN; the cloud is only used once to fetch the local key.

## What's here

| Path | What |
|---|---|
| `acweb/` | Web interface: live values, every setting, **Max heat** and **Max cold** buttons, change log |
| `monitor.py` | Terminal monitor that prints every value change the AC pushes |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m tinytuya wizard      # Tuya IoT project Access ID/Secret, region eu, scan: No
```

The wizard writes `devices.json` (contains the AC's **local key**) and `tinytuya.json`
(contains your **API secret**). Both are in `.gitignore`; never commit them.

## Web interface

```bash
AC_NAME="Oficina AC" .venv/bin/python -m acweb.server
```

Open http://127.0.0.1:8320/. It keeps one connection to the AC, refreshes every 2 s, and shows
for each command what the AC reports back, so refused settings are visible.

| Variable | Default | |
|---|---|---|
| `AC_IP` | `192.168.1.84` | AC's LAN address |
| `AC_ID` | first `kt` device in `devices.json` | Tuya device id |
| `AC_VERSION` | `3.5` | Tuya local protocol |
| `AC_HOST` / `AC_PORT` | `127.0.0.1` / `8320` | Where the page listens. There is no login: keep it on localhost or a trusted network. |
| `AC_NAME` | `Air conditioner` | Page title |

### Presets

| | Max heat | Max cold |
|---|---|---|
| Power / mode | on, heat | on, cool |
| Target | 30 °C (max) | 16 °C (min) |
| ECO, quiet, sleep | off | off |
| Turbo | on (sets fan to auto and ramps it itself) | on |
| Electric aux heater | on — this unit has none and refuses it | off |

Settings are sent one at a time and each is checked against what the AC reports afterwards.

## Device quirks

- **Temperatures are raw Haier values**, not the °C the Tuya model claims:
  room (DP 3) = raw ÷ 2, outdoor (DP 128) = raw − 64. The room reading jumps when the unit is
  switched off and isn't reliable then.
- **Turbo overrides fan speed** and forces `auto`.
- **Electric aux heater (DP 12)** is refused: the unit has no heater element.
- DPs 23, 24, 115, 121–126, 135–137 exist in the product template but this unit doesn't report them.
