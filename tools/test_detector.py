"""Offline check of detector/detect.py on a real clip with synthetic events.

  .venv/bin/python tools/test_detector.py <folder of PNG frames from the substream> [person.png]

Each scenario replays the clip (6 fps) through the same motion code the container runs and reports
whether an event started. Light changes, shadows, flicker and PTZ moves must not; a person must.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "detector"))
import detect  # noqa: E402


class FakeStore:
    def __init__(self):
        self.started = []

    def start(self, cam, now):
        e = dict(id=len(self.started))
        self.started.append(now)
        return e

    def finish(self, e, now):
        pass

    def update(self, *a):
        pass


def run(frames, edit, lenses=2):
    store = FakeStore()
    cam = detect.Camera("test", "", "", lenses, None, store)
    first = None
    for i, f in enumerate(frames):
        f = edit(f.copy(), i) if i >= 20 else f        # 20 frames to learn the background
        cam.analyse(f, i / 6)
        if cam.state["motion"] and first is None:
            first = i
    return first, store.started


def soft_mask(shape, cx, cy, rx, ry):
    m = np.zeros(shape[:2], np.float32)
    cv2.ellipse(m, (int(cx), int(cy)), (int(rx), int(ry)), 0, 0, 360, 1, -1)
    return cv2.GaussianBlur(m, (0, 0), 9)[..., None]


def main():
    folder = Path(sys.argv[1])
    frames = [cv2.imread(str(p)) for p in sorted(folder.glob("*.png"))][40:]
    frames = (frames + frames[::-1]) * 2                      # ping-pong to ~30 s
    H, W = frames[0].shape[:2]
    person = cv2.imread(sys.argv[2]) if len(sys.argv) > 2 else None
    rng = np.random.default_rng(1)

    def scale(f, k):
        return np.clip(f.astype(np.float32) * k, 0, 255).astype(np.uint8)

    def shadow(f, i):        # soft shadow 45 % darker crossing the floor of the lower lens
        m = soft_mask(f.shape, 40 + (i - 20) * 9, H * 0.8, 90, 45)
        return np.clip(f * (1 - 0.45 * m), 0, 255).astype(np.uint8)

    def sunspot(f, i):       # bright patch (sun through a window) drifting on the upper lens
        m = soft_mask(f.shape, 100 + (i - 20) * 6, H * 0.3, 70, 50)
        return np.clip(f * (1 + 0.6 * m), 0, 255).astype(np.uint8)

    def ptz(f, i):           # lower lens pans 4 px per frame for 2 s
        s = min(i - 20, 12) * 4
        out = f.copy()
        out[H // 2:] = np.roll(f[H // 2:], s, axis=1)
        return out

    def walker(f, i, ph=150, pw=55, speed=6, y=0.62):   # a person crossing the lower lens
        p = cv2.resize(person, (pw, ph))
        x, y = 30 + int((i - 20) * speed), int(H * y)
        if x + pw < W:
            f[y:y + ph, x:x + pw] = p
        return f

    scenarios = [
        ("real clip, nothing added", lambda f, i: f, False),
        ("lights dim to 55 % over 1 s", lambda f, i: scale(f, max(0.55, 1 - (i - 20) * 0.075)), False),
        ("lights switched on (+40 % at once)", lambda f, i: scale(f, 1.4 if i >= 40 else 1), False),
        ("fluorescent flicker ±8 % per frame", lambda f, i: scale(f, 1 + rng.uniform(-0.08, 0.08)), False),
        ("soft shadow moving across the floor", shadow, False),
        ("sun patch drifting over a wall", sunspot, False),
        ("PTZ lens panning", ptz, False),
    ]
    if person is not None:
        scenarios += [
            ("person walking through", walker, True),
            ("small, distant person (60 px tall)", lambda f, i: walker(f, i, 60, 22, 3, 0.7), True),
            ("person walking slowly (1 px/frame)", lambda f, i: walker(f, i, speed=1), True),
            ("person entering while lights dim", lambda f, i: walker(scale(f, max(0.6, 1 - (i - 20) * 0.03)), i), True),
        ]
    ok = True
    for name, edit, want in scenarios:
        first, events = run(frames, edit)
        good = bool(events) == want
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {name:40s} events={len(events)}"
              + (f" (first at {first / 6:.1f} s)" if first is not None else ""))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
