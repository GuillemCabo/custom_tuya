"""Tune an iCSee camera's image settings for the best measured quality (see imgq.py).

Coordinate search: for each setting, measure the current value and the candidates (median of
several full-resolution frames each) and keep a candidate only if it beats the current value by
MARGIN, so frame-to-frame noise doesn't move settings around. Geometry settings (mirror, flip,
corridor, lens correction, stabilisation) are never touched.

  .venv/bin/python tools/tune_image.py [camera id]    → writes tuning/ (gitignored)
"""
import json
import statistics
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import imgq  # noqa: E402
from acweb.cameras import XMCamera  # noqa: E402

GO2RTC, FRAMES, MARGIN, SETTLE = "http://127.0.0.1:8321", 4, 1.0, 3.0
OUT = Path("tuning")
BACKUP = ["Camera.Param.[0]", "Camera.ParamEx.[0]", "AVEnc.VideoColor.[0]", "Camera.ClearFog", "Simplify.Encode"]
CANDIDATES = [  # (setting key, values to try); order matters: exposure first, then tone, then detail
    ("aesens", [3, 5, 7, 9]),
    ("overexpo", [False, True]),
    ("blc", [False, True]),
    ("brightness", [40, 45, 50, 55, 60]),
    ("contrast", [40, 45, 50, 55, 60, 65]),
    ("dnr_day", [1, 2, 3, 4, 5]),
    ("style", ["type1", "type2", "type3"]),
    ("defog", [False, True]),
    ("saturation", [45, 50, 55, 60, 65]),
    ("main_br", [1536, 2560, 4096]),
    ("main_q", [3, 4, 5, 6]),
]


def total(m):
    # imgq.score is lower-is-better; a little credit for colourfulness (BRISQUE only sees grey)
    return m["score"] - 0.1 * m["colour"]


def measure(stream, n=FRAMES):
    runs, last = [], None
    for _ in range(n):
        last = imgq.grab(GO2RTC, stream)
        runs.append(imgq.evaluate(last))
    med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    med["total"] = statistics.median(total(r) for r in runs)
    return med, last


def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else "icsee_xm535"
    cfg = json.loads(Path("cameras.json").read_text())[cid]
    cam = XMCamera(cid, cfg)
    OUT.mkdir(exist_ok=True)
    (OUT / "backup.json").write_text(json.dumps({n: cam.dev.get(n) for n in BACKUP}, indent=1))
    value = {f["key"]: f["value"] for f in cam.settings()["fields"]}
    log = []
    before, img = measure(cid, 6)
    cv2.imwrite(str(OUT / "before.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"before: total {before['total']:.2f}  {json.dumps({k: round(v, 3) for k, v in before.items()})}", flush=True)
    for key, values in CANDIDATES:
        if key not in value:
            continue
        current = value[key]
        results = {}
        for v in [current] + [x for x in values if x != current]:
            if v != current or results:
                cam.set(key, v)
            time.sleep(SETTLE * (2 if key.startswith("main_") else 1))
            results[json.dumps(v)] = measure(cid)[0]["total"]
            print(f"  {key}={v}: {results[json.dumps(v)]:.2f}", flush=True)
        best = min(results, key=results.get)
        keep = json.loads(best) if results[best] < results[json.dumps(current)] - MARGIN else current
        cam.set(key, keep)
        value[key] = keep
        log.append(dict(key=key, before=current, after=keep, scores=results))
        print(f"{key}: {current} → {keep}", flush=True)
    time.sleep(SETTLE)
    after, img = measure(cid, 6)
    cv2.imwrite(str(OUT / "after.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"after: total {after['total']:.2f}  {json.dumps({k: round(v, 3) for k, v in after.items()})}", flush=True)
    (OUT / "result.json").write_text(json.dumps(dict(before=before, after=after, steps=log), indent=1))


if __name__ == "__main__":
    main()
