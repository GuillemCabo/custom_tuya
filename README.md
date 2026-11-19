# custom_tuya

Local control and monitoring for a Haier Wi‑Fi air conditioner on the Tuya platform, plus a camera
page for iCSee (Xiongmai) and Tuya cameras (see [Cameras](#cameras)). The AC is
(Tuya product `9xvzf8c0bg33eenj`, "海尔家用空调（全功能）", local protocol 3.5).
Everything talks to the AC directly on the LAN; the cloud is only used once to fetch the local key.

## What's here

| Path | What |
|---|---|
| `acweb/` | Web interface: live values, every setting, **Max heat** and **Max cold** buttons, change log; cameras on `/cameras` |
| `acweb/cameras.py`, `acweb/dvrip.py` | Camera backend: settings and PTZ for iCSee cameras (local DVRIP protocol), Tuya cameras through the cloud |
| `docker-compose.yml`, `go2rtc/` | go2rtc container that turns camera streams into something browsers play |
| `detector/` | `tuya-detect` container: motion and person detection that ignores light changes and shadows |
| `tools/` | Image tuning (`tune_image.py`), SD-card recording scan (`scan_recordings.py`), detector test (`test_detector.py`) |
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

## Cameras

Open http://127.0.0.1:8320/cameras. The grid shows every camera live; click one for the full
stream, pan/tilt/zoom with presets and patrol, and a settings panel (image, night vision and white
light, detection, video encoding, on-screen text, volume, clock, reboot, plus a raw JSON editor).
Every change is read back from the camera and the page shows what it reports.

```bash
docker compose up -d        # go2rtc on 127.0.0.1:8321 (players/API) and :8322 (RTSP re-stream)
```

**iCSee cameras** go in `cameras.json` (gitignored; holds the logins):

```json
{ "icsee_xm535": { "ip": "192.168.1.10", "user": "…", "password": "…" } }
```

The login is the device login from the iCSee app (device settings → password). Video comes from
the camera's RTSP server and settings from its DVRIP port (34567). acweb registers the streams with
go2rtc through its API every 20 s, so they come back by themselves when go2rtc restarts, and no
password is written to the tracked `go2rtc/go2rtc.yaml` (it is mounted read-only).

**Video conversion.** iCSee cameras send H.265 only. Browsers that decode H.265 (Chrome/Edge with
hardware support) get it untouched; others make go2rtc start ffmpeg and transcode to H.264, which
costs about two CPU cores per viewer for the 6 MP main stream (the grid uses the small substream).
ffmpeg stops when nobody is watching. Audio (G.711 A-law) is converted by go2rtc itself.

**Tuya cameras** (doorbells, `sp`/`dmsxj`/`sxt` categories in `devices.json`) show their cloud
settings. Live video needs the **IoT Video Live Stream** service on the Tuya IoT cloud project
(Cloud → your project → Service API → Go to authorise); until then "Try live video" says so.
Battery doorbells sleep and only stream while awake.

Security: neither the page nor go2rtc has a login, and go2rtc's API lists stream URLs including
camera passwords. Both listen on 127.0.0.1 only; keep it that way.

| Variable | Default | |
|---|---|---|
| `CAMERAS` | `cameras.json` | iCSee camera list |
| `GO2RTC_URL` | `http://127.0.0.1:8321` | go2rtc API as seen from acweb (the page uses the same port on the host it was opened from) |

## Motion and person detection

`docker compose up -d` also starts `tuya-detect` (API on 127.0.0.1:8323). Models are not in git:
run `tools/get_models.sh` once (YOLOX-S person detector, Apache-2.0, plus the BRISQUE files used
by the tuner). The camera page shows a Motion/Person badge on each tile, an overlay of what the
detector sees on the live video (▣ Detection), and recent events with snapshots.

How it works (details at the top of `detector/detect.py`):

- Each lens of the substream is compared with a learnt background in 8×8 blocks, about 4–6 times a
  second. Overall light changes (auto-exposure, lights dimming, image-setting changes) are removed
  first with a gain and offset fitted on brightness percentiles.
- Blocks are compared by texture (normalised correlation after removing smooth light gradients),
  not brightness: shadows and light patches keep the texture underneath; objects hide it. Plain
  surfaces only count when their colour changes too, or they get darker than a shadow can make them.
  Washed-out or crushed blocks are treated as lighting.
- A moving PTZ lens (whole-image shift, by phase correlation) or a whole-scene change (lights, IR
  switch) makes it re-learn the background instead of alarming.
- Changes must persist (2 of 3 frames, 3 frames to start an event); things that stop moving are
  absorbed after a minute or two.
- Moving areas are checked for people on full-resolution frames, using a square crop as tall as the
  lens (tight crops score far worse). A person only counts if it overlaps the motion, so posters,
  coats or a post that looks like a person never alarm.

`tools/test_detector.py <frames dir> <person.png>` replays a real clip with synthetic events: light
dimming/switching, flicker, a moving shadow, a drifting sun patch and PTZ panning must not trigger;
a person walking (also small, slow, or while lights dim) must.

## Image tuning

`tools/tune_image.py` searches the image settings one at a time for the best measured quality
(BRISQUE no-reference score plus penalties for blown highlights and crushed shadows, median of
several full-resolution frames) and keeps a change only when it clearly wins. It backs up the
original settings to `tuning/backup.json`. Check the result by eye: the score rewarded high
saturation that only amplified a blue cast, so saturation was set back by hand.

## Scanning recordings

`tools/scan_recordings.py "<from>" "<to>"` downloads the camera's SD-card recordings over DVRIP
(live view and settings are slow meanwhile), unpacks Xiongmai's `.h264` container
(`tools/xmfile.py`), and runs the same motion model on every keyframe (~3 s apart) with person
checks, plus a brute-force whole-image person search on every 20th keyframe for comparison.
Results go to `recordings/scan/<camera>/` (gitignored): `events.json` and snapshots.

Recordings are scanned slowly (about 300 KB/s, one file at a time) because faster downloads made the
camera stop answering until it was power-cycled; the scan stops by itself if the camera stalls and
resumes where it left off. Motion clips (`[M]`, about 12 s) go first. The learnt background is kept
across gaps between files, so a short clip is compared with the empty room rather than spending its
few keyframes learning it.
