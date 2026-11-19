"""Motion and person detector for indoor cameras, robust to light changes and shadows.

Runs as the `tuya-detect` container. For each camera it reads the small substream from go2rtc and
looks for real movement; when something moves it grabs a full-resolution frame and runs a person
detector (YOLOX-S) on crops around the moving areas.

Why it ignores light changes and shadows
  1. Global light level: the frame is mapped onto the background with a gain and an offset fitted
     on brightness percentiles (robust to a moving object), so auto-exposure, clouds, lights dimming
     and image-setting changes (brightness, contrast) don't count.
  2. Texture, not brightness: each block is compared with the background by normalised
     cross-correlation (NCC). A shadow or a light patch scales the brightness of what is already
     there but keeps its texture, so NCC stays high; an object covers the texture and NCC drops.
     The block must also differ clearly after that correction, so compression refreshes and noise
     reduction smoothing texture don't count. Blocks washed out to white by a light patch (or
     crushed to black by a deep shadow) have no texture left and are treated as lighting.
     Texture is measured after removing the smooth brightness gradient within each block, since
     light falls off smoothly while objects have structure. Plain blocks (no texture to compare)
     only count when their colour changes too (shadows and light keep a surface's colour) and the
     brightness changes more than a shadow can or real edges appear, or when they get far darker
     than a shadow could make them.
  3. Persistence: a block must change in 2 of the last 3 frames, regions must be big enough, and an
     event needs 3 consecutive frames with motion, so flicker and noise don't trigger.
  4. Whole-scene changes (lights switched, IR/day switch) affect most blocks at once, and a moving
     PTZ lens shifts the whole image (measured by phase correlation): in both cases the background
     is re-learnt quickly instead of raising an alarm.
  5. Something that stops moving (a parked car) is absorbed into the background after ~1-2 minutes.

Persons only count when the detection overlaps a moving area, so posters, coats on hooks or
mannequins that look like people never raise an alarm. Each camera may be several lenses stacked
vertically (LENSES); every lens gets its own background.

HTTP API (port 8000 in the container):
  GET /api/state             live state of every camera (regions/persons in 0..1 frame coordinates)
  GET /api/events[?camera=]  recent events, newest first
  GET /events/<id>.jpg       snapshot of an event with boxes drawn
  GET /api/debug.jpg?camera= substream frame with the block mask, for checking sensitivity
"""
import json
import logging
import os
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

log = logging.getLogger("detect")
GO2RTC_HTTP = os.environ.get("GO2RTC_HTTP", "http://go2rtc:1984")
GO2RTC_RTSP = os.environ.get("GO2RTC_RTSP", "rtsp://go2rtc:8554")
DATA = Path(os.environ.get("DATA", "/data"))
MODEL = os.environ.get("MODEL", "/models/yolox_s.onnx")
# camera id:analysis stream:full-res stream:lenses, comma separated
CAMERAS = os.environ.get("CAMERAS", "icsee_xm535:icsee_xm535_sub:icsee_xm535:2")
ANALYSE_FPS = float(os.environ.get("ANALYSE_FPS", "6"))
# a person must also overlap real motion, which removes most false alarms, so the model's own
# threshold can be lowish (helps with people half hidden or against bright windows)
PERSON_CONF = float(os.environ.get("PERSON_CONF", "0.35"))
KEEP_EVENTS = int(os.environ.get("KEEP_EVENTS", "500"))

# motion parameters (analysis image is each lens resized to WORK_W wide)
WORK_W, BLOCK = 320, 8
NCC_CHANGED = 0.55           # textured block counts as changed below this correlation
TEXTURE = 6.0                # grey-level std that makes a block "textured"
RESIDUAL = 12.0              # textured blocks must also differ by this much (rms grey levels)
SHADOW_RANGE = (0.45, 1.8)   # brightness ratio a shadow / light patch can cause on a flat block
MIN_DIFF = 10.0              # ...and the absolute mean difference that is noise
CHROMA = 0.025               # colour shift (in r,g chromaticity) above which a plain block "changed colour"
MIN_REGION_BLOCKS = 3
SCENE_CHANGE = 0.35          # fraction of blocks changing at once = lights/IR/PTZ, not motion
CAMERA_SHIFT = 1.0           # whole-image shift between frames (px at WORK_W) = the camera moved
EVENT_START_FRAMES, EVENT_END_S = 3, 4.0
ALPHA_BG, ALPHA_FG, ALPHA_RELEARN = 0.04, 0.0015, 0.5
RELEARN_FRAMES = 15          # frames of fast background learning after a scene change

INPUT = 640
STRIDES = (8, 16, 32)


class PersonDetector:
    """YOLOX-S (COCO, OpenCV model zoo build): person class only."""

    def __init__(self, path):
        self.net = cv2.dnn.readNetFromONNX(path)
        grids, strides = [], []
        for s in STRIDES:
            n = INPUT // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grids, self.strides = np.concatenate(grids), np.concatenate(strides)
        self.lock = threading.Lock()

    def detect(self, bgr, conf=PERSON_CONF):
        """Return [(x, y, w, h, score)] in bgr pixel coordinates."""
        h, w = bgr.shape[:2]
        r = min(INPUT / h, INPUT / w)
        canvas = np.full((INPUT, INPUT, 3), 114, np.uint8)
        canvas[:int(h * r), :int(w * r)] = cv2.resize(bgr, (int(w * r), int(h * r)), interpolation=cv2.INTER_AREA)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None].astype(np.float32)
        with self.lock:
            self.net.setInput(blob)
            out = self.net.forward()[0]
        xy = (out[:, :2] + self.grids) * self.strides
        wh = np.exp(out[:, 2:4]) * self.strides
        score = out[:, 4] * out[:, 5]          # objectness × person class
        keep = score >= conf
        boxes = np.concatenate([xy[keep] - wh[keep] / 2, wh[keep]], 1) / r
        scores = score[keep]
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), conf, 0.45)
        return [(*boxes[i].tolist(), float(scores[i])) for i in np.array(idx).flatten()]


def _detrend_matrix(n=BLOCK):
    """64×64 matrix removing the best-fitting plane (a + bx + cy) from a block: what's left is
    texture, not a smooth light gradient."""
    yy, xx = np.mgrid[0:n, 0:n]
    X = np.stack([np.ones(n * n), xx.ravel(), yy.ravel()], 1)
    return (np.eye(n * n) - X @ np.linalg.pinv(X)).astype(np.float32)


DETREND = _detrend_matrix()


class LensMotion:
    """Background model and block classification for one lens."""

    def __init__(self):
        self.bg = self.bgc = self.prev = None
        self.history = deque(maxlen=3)
        self.relearn = 0

    @staticmethod
    def blocks(img):
        h, w = img.shape[:2]
        b = img[:h - h % BLOCK, :w - w % BLOCK]
        return b.reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK, *img.shape[2:]).swapaxes(1, 2)

    _windows = {}

    @classmethod
    def window(cls, shape):
        if shape not in cls._windows:
            cls._windows[shape] = cv2.createHanningWindow(shape[::-1], cv2.CV_32F)
        return cls._windows[shape]

    def update(self, gray, chroma):
        """gray: float32 image of this lens; chroma: its r/(r+g+b), g/(r+g+b) channels.
        Returns (block mask, scene_change flag)."""
        if self.bg is None or self.bg.shape != gray.shape:
            self.bg, self.bgc, self.relearn = gray.copy(), chroma.copy(), RELEARN_FRAMES
            self.history.clear()
            self.prev = gray
        # 0. camera moving (PTZ): the whole image shifted since the last frame
        (dx, dy), _ = cv2.phaseCorrelate(self.prev, gray, self.window(gray.shape))
        self.prev = gray
        camera_moved = np.hypot(dx, dy) > CAMERA_SHIFT
        # 1. global light level: gain + offset from matching brightness percentiles
        pc, pb = np.percentile(gray, (20, 50, 80)), np.percentile(self.bg, (20, 50, 80))
        gain = min(max((pc[2] - pc[0]) / max(pb[2] - pb[0], 1.0), 0.2), 5.0)
        cur = (gray - pc[1]) / gain + pb[1]
        # 2. per block: texture with smooth light gradients removed, its correlation with the
        #    background, brightness ratio and colour change
        cb, bb = self.blocks(cur), self.blocks(self.bg)
        bh, bw = cb.shape[:2]
        cz = cb.reshape(bh, bw, -1) @ DETREND
        bz = bb.reshape(bh, bw, -1) @ DETREND
        cm, bm = cb.mean((2, 3)), bb.mean((2, 3))
        cs, bs = np.sqrt((cz ** 2).mean(2)), np.sqrt((bz ** 2).mean(2))
        ncc = (cz * bz).mean(2) / (cs * bs + 1e-6)
        textured = (cs > TEXTURE) & (bs > TEXTURE)
        ratio = (cm + 1) / (bm + 1)
        residual = np.sqrt(((cz - bz) ** 2).mean(2))
        dchroma = np.abs(self.blocks(chroma).mean((2, 3)) - self.blocks(self.bgc).mean((2, 3))).max(2)
        lit = (cm > 30) & (bm > 30)
        light_like = lit & (dchroma < CHROMA)            # same colour, different brightness: lighting
        flat_changed = (~textured) & (
            (~light_like & (((ratio < SHADOW_RANGE[0]) | (ratio > SHADOW_RANGE[1])) & (np.abs(cm - bm) > MIN_DIFF)
                            | (cs > bs + 2 * TEXTURE)))  # edges appeared on a plain surface
            | ((ratio < 0.3) & (np.abs(cm - bm) > 40)))  # far darker than any shadow
        changed = (textured & (ncc < NCC_CHANGED) & (residual > RESIDUAL)) | flat_changed
        # washed out by a light patch / crushed by a deep shadow: no texture left to compare
        changed &= ~(((cm > 225) & (ratio > 1)) | ((cm < 15) & (ratio < 1)))
        scene_change = changed.mean() > SCENE_CHANGE or camera_moved
        if scene_change:
            self.relearn = RELEARN_FRAMES
        self.changed = changed & (self.relearn == 0)   # this frame alone, before persistence
        # 3. persistence: changed in 2 of the last 3 frames
        self.history.append(changed)
        mask = (np.sum(self.history, 0) >= 2) if len(self.history) >= 2 else np.zeros_like(changed)
        # background update: fast while re-learning, slow under moving things
        if self.relearn:
            self.relearn -= 1
            a = ALPHA_RELEARN
            mask = np.zeros_like(mask)
        else:
            a = np.where(np.kron(mask, np.ones((BLOCK, BLOCK))).astype(bool), ALPHA_FG, ALPHA_BG)
            a = np.pad(a, ((0, gray.shape[0] - a.shape[0]), (0, gray.shape[1] - a.shape[1])),
                       constant_values=ALPHA_BG).astype(np.float32)
        self.bg += a * (gray - self.bg)
        self.bgc += (a[..., None] if np.ndim(a) else a) * (chroma - self.bgc)
        return mask, scene_change or self.relearn > 0


def regions(mask):
    """Connected areas of changed blocks → [(x, y, w, h)] in block units."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)), 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if np.count_nonzero(mask[y:y + h, x:x + w]) >= MIN_REGION_BLOCKS:
            out.append((int(x), int(y), int(w), int(h)))
    return out


class Camera(threading.Thread):
    def __init__(self, cid, stream, hires, lenses, detector, store):
        super().__init__(daemon=True, name=f"cam-{cid}")
        self.id, self.stream, self.hires, self.lenses = cid, stream, hires, lenses
        self.detector, self.store = detector, store
        self.motion = [LensMotion() for _ in range(lenses)]
        self.state = dict(camera=cid, connected=False, motion=False, human=False, scene_change=False,
                          regions=[], persons=[], fps=0.0, event=None, error=None, updated=None)
        self.last_frame, self.last_mask = None, None
        self.streak, self.last_motion, self.event, self.checking = 0, 0.0, None, False

    def run(self):
        while True:
            cap = cv2.VideoCapture(f"{GO2RTC_RTSP}/{self.stream}", cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.state.update(connected=False, error=f"cannot open {self.stream} from go2rtc")
                time.sleep(5)
                continue
            self.state.update(connected=True, error=None)
            last, times = 0.0, deque(maxlen=20)
            while True:
                ok, frame = cap.read()
                if not ok:
                    self.state.update(connected=False, error="stream ended, reconnecting")
                    break
                now = time.time()
                if now - last < 1 / ANALYSE_FPS:
                    continue
                last = now
                times.append(now)
                try:
                    self.analyse(frame, now)
                except Exception:  # noqa: BLE001
                    log.exception("analysis failed")
                if len(times) > 1:
                    self.state["fps"] = round((len(times) - 1) / (times[-1] - times[0]), 1)
            cap.release()
            time.sleep(2)

    def analyse(self, frame, now):
        h = frame.shape[0] // self.lenses
        found, masks, scene = [], [], False
        for i, lens in enumerate(self.motion):
            part = frame[i * h:(i + 1) * h]
            scale = WORK_W / part.shape[1]
            small = cv2.GaussianBlur(cv2.resize(part, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA),
                                     (3, 3), 0).astype(np.float32)
            g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            chroma = small[..., [2, 1]] / (small.sum(2, keepdims=True) + 1)
            mask, sc = lens.update(g, chroma)
            scene |= sc
            masks.append(mask)
            bh, bw = mask.shape
            for x, y, w, hh in regions(mask):
                # → 0..1 coordinates of the whole (stacked) frame
                found.append([x / bw, (i + y / bh) / self.lenses, w / bw, hh / bh / self.lenses])
        self.last_frame, self.last_mask = frame, masks
        self.streak = self.streak + 1 if found else 0
        moving = self.streak >= EVENT_START_FRAMES
        if moving:
            self.last_motion = now
        active = moving or (self.event is not None and now - self.last_motion < EVENT_END_S)
        if active and self.event is None:
            self.event = self.store.start(self.id, now)
        if self.event is not None and not active:
            self.store.finish(self.event, now)
            self.event = None
        self.state.update(motion=active, scene_change=scene, regions=found if active else [],
                          updated=datetime.now().isoformat(timespec="seconds"),
                          event=self.event["id"] if self.event else None)
        if not active:
            self.state.update(human=False, persons=[])
        elif found and not self.checking and self.detector:
            self.checking = True
            threading.Thread(target=self.check_persons, args=(list(found), self.event), daemon=True).start()

    def check_persons(self, found, event):
        """Grab a full-resolution frame and look for people around the moving areas."""
        try:
            with urllib.request.urlopen(f"{GO2RTC_HTTP}/api/frame.jpeg?src={self.hires}", timeout=10) as r:
                img = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
            H, W = img.shape[:2]
            persons = []
            for x0, y0, side in crops_for(found, W, H, self.lenses):
                for bx, by, bw, bh, sc in self.detector.detect(img[y0:y0 + side, x0:x0 + side]):
                    box = [(x0 + bx) / W, (y0 + by) / H, bw / W, bh / H]
                    if any(overlaps(box, r) for r in found) and not any(iou(box, p["box"]) > 0.5 for p in persons):
                        persons.append(dict(box=box, score=round(sc, 2)))
            if self.event is event and event is not None:
                self.state.update(persons=persons, human=bool(persons) or self.state["human"])
                self.store.update(event, img, found, persons)
        except Exception as e:  # noqa: BLE001
            log.warning("person check failed for %s: %s", self.id, e)
        finally:
            self.checking = False

    def debug_jpeg(self):
        if self.last_frame is None:
            return None
        img = self.last_frame.copy()
        h = img.shape[0] // self.lenses
        for i, m in enumerate(self.last_mask or []):
            sy, sx = h / m.shape[0], img.shape[1] / m.shape[1]
            for by, bx in zip(*np.nonzero(m)):
                cv2.rectangle(img, (int(bx * sx), int(i * h + by * sy)), (int((bx + 1) * sx), int(i * h + (by + 1) * sy)),
                              (0, 140, 255), 1)
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def crops_for(found, W, H, lenses):
    """Square crops (x0, y0, side) to check for people: per lens, one square as tall as the lens
    centred on each moving area, merged when they overlap a lot. Whole-lens-height squares give
    the person detector enough context; tight crops around the motion score much worse."""
    lh = H // lenses
    side = min(lh, W)
    out = []
    for x, y, w, h in found:
        lens = min(int((y + h / 2) * lenses), lenses - 1)
        cx = (x + w / 2) * W
        x0 = int(min(max(cx - side / 2, 0), W - side))
        y0 = lens * lh
        if not any(c[1] == y0 and abs(c[0] - x0) < side * 0.4 for c in out):
            out.append((x0, y0, side))
    return out


def overlaps(box, region, pad=0.03):
    x, y, w, h = box
    rx, ry, rw, rh = region
    return x < rx + rw + pad and x + w > rx - pad and y < ry + rh + pad and y + h > ry - pad


def iou(a, b):
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


class Store:
    """Events in DATA/events.json and snapshots in DATA/<id>.jpg."""

    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.file = DATA / "events.json"
        self.events = json.loads(self.file.read_text()) if self.file.exists() else []
        self.lock = threading.Lock()

    def save(self):
        with self.lock:
            self.events = self.events[:KEEP_EVENTS]
            keep = {e["id"] for e in self.events}
            for f in DATA.glob("*.jpg"):
                if f.stem not in keep:
                    f.unlink(missing_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.events))
            tmp.replace(self.file)

    def start(self, camera, now):
        e = dict(id=datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S-") + camera, camera=camera,
                 start=datetime.fromtimestamp(now).isoformat(timespec="seconds"), end=None,
                 kind="motion", persons=0, best=0.0, snapshot=False)
        self.events.insert(0, e)
        self.save()
        return e

    def update(self, e, img, found, persons):
        better = len(persons) > e["persons"] or (persons and max(p["score"] for p in persons) > e["best"])
        if not e["snapshot"] or better:
            H, W = img.shape[:2]
            out = img.copy()
            for x, y, w, h in found:
                cv2.rectangle(out, (int(x * W), int(y * H)), (int((x + w) * W), int((y + h) * H)), (0, 140, 255), 3)
            for p in persons:
                x, y, w, h = p["box"]
                cv2.rectangle(out, (int(x * W), int(y * H)), (int((x + w) * W), int((y + h) * H)), (60, 220, 60), 5)
                cv2.putText(out, f"person {p['score']:.2f}", (int(x * W), max(30, int(y * H) - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.4, (60, 220, 60), 3)
            out = cv2.resize(out, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(DATA / f"{e['id']}.jpg"), out, [cv2.IMWRITE_JPEG_QUALITY, 82])
            e["snapshot"] = True
        if persons:
            e.update(kind="person", persons=max(e["persons"], len(persons)),
                     best=max(e["best"], max(p["score"] for p in persons)))
        self.save()

    def finish(self, e, now):
        e["end"] = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        self.save()


CAMS = {}
STORE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/state":
            return self.send(200, {c.id: c.state for c in CAMS.values()})
        if url.path == "/api/events":
            ev = [e for e in STORE.events if not q.get("camera") or e["camera"] == q["camera"]]
            return self.send(200, ev[:int(q.get("limit", 50))])
        if url.path.startswith("/events/") and url.path.endswith(".jpg"):
            f = DATA / Path(url.path).name
            return self.send(200, f.read_bytes(), "image/jpeg") if f.exists() else self.send(404, {"error": "no snapshot"})
        if url.path == "/api/debug.jpg" and q.get("camera") in CAMS:
            jpg = CAMS[q["camera"]].debug_jpeg()
            return self.send(200, jpg, "image/jpeg") if jpg else self.send(503, {"error": "no frame yet"})
        self.send(404, {"error": "not found"})


def main():
    global STORE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    STORE = Store()
    detector = PersonDetector(MODEL) if Path(MODEL).exists() else None
    if not detector:
        log.warning("no person model at %s: motion only", MODEL)
    for spec in filter(None, (s.strip() for s in CAMERAS.split(","))):
        cid, stream, hires, lenses = (spec.split(":") + ["", "", "1"])[:4]
        cam = Camera(cid, stream, hires or stream, int(lenses or 1), detector, STORE)
        CAMS[cid] = cam
        cam.start()
    log.info("detector API on :8000 for %s", ", ".join(CAMS))
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
