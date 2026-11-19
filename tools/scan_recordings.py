"""Download a camera's SD-card recordings for a time range and run the detector over them.

  .venv/bin/python tools/scan_recordings.py "2026-11-14 00:00:00" "2026-11-15 23:59:59" [camera id]

Files land in recordings/<date>/ (gitignored) and are analysed in time order as they arrive:
  * every keyframe (~3 s apart) goes through the same motion model as the live detector
    (detector/detect.py: ignores light changes, shadows, PTZ moves);
  * moving areas are checked for people on the full-resolution frame;
  * every CHECK_EVERY-th keyframe also gets a brute-force person search over the whole image,
    ignoring motion, to measure what the motion gating misses.
Results: recordings/scan/<camera>/ — per-file JSON, events.json, snapshots of person events.
Re-running skips files already downloaded and analysed.
"""
import json
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "detector"), str(ROOT / "tools")]
import detect  # noqa: E402
import xmfile  # noqa: E402
from acweb.dvrip import DVRIP  # noqa: E402

WORKERS, CHECK_EVERY, EVENT_GAP = 2, 20, 30  # 2 is fastest; live view and settings are slow meanwhile
LENSES = 2
# keyframes are ~3 s apart: faster background learning per frame than the live 6 fps detector
detect.ALPHA_BG, detect.ALPHA_FG, detect.RELEARN_FRAMES = 0.25, 0.01, 3


def local_name(f):
    return Path(f["FileName"].split("/")[-3] if f["FileName"].count("/") >= 3 else "") / Path(f["FileName"]).name


def downloader(cfg, jobs, done, out_dir):
    dev = DVRIP(cfg["ip"], cfg["user"], cfg["password"])
    while True:
        try:
            i, f = jobs.get_nowait()
        except queue.Empty:
            return
        path = out_dir / local_name(f)
        want = int(f["FileLength"], 16) * 1024
        for attempt in range(3):
            if path.exists() and path.stat().st_size >= want:
                break
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            try:
                with open(tmp, "wb") as o:
                    dev.download(f, o)
                tmp.replace(path)
            except Exception as e:  # noqa: BLE001
                print(f"  download retry {attempt + 1} {path.name}: {e}", flush=True)
                dev.close()
                time.sleep(3)
        done[i] = path


def tiles(img, n=2):
    """Overlapping n×n tiles of each lens, for the brute-force check."""
    H, W = img.shape[:2]
    lh = H // LENSES
    for lens in range(LENSES):
        th, tw = int(lh / n * 1.2), int(W / n * 1.2)
        for r in range(n):
            for c in range(n):
                y0 = lens * lh + min(int(r * lh / n), lh - th)
                x0 = min(int(c * W / n), W - tw)
                yield x0, y0, img[y0:y0 + th, x0:x0 + tw]


def persons_in(det, img, boxes_px, conf=detect.PERSON_CONF):
    H, W = img.shape[:2]
    out = []
    for x0, y0, crop in boxes_px:
        for bx, by, bw, bh, s in det.detect(crop, conf):
            box = [(x0 + bx) / W, (y0 + by) / H, bw / W, bh / H]
            if not any(detect.iou(box, p["box"]) > 0.4 for p in out):
                out.append(dict(box=[round(v, 4) for v in box], score=round(s, 2)))
    return out


def analyse(path, lenses, det, start_index):
    """Run motion + person checks over one file's keyframes. Returns list of keyframe records."""
    data = path.read_bytes()
    records = []
    for k, (when, img) in enumerate(xmfile.keyframes(data)):
        if when is None:
            continue
        H, W = img.shape[:2]
        lh = H // LENSES
        found = []
        for i, lens in enumerate(lenses):
            part = img[i * lh:(i + 1) * lh]
            scale = detect.WORK_W / W
            small = cv2.GaussianBlur(cv2.resize(part, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA),
                                     (3, 3), 0).astype(np.float32)
            g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            lens.update(g, small[..., [2, 1]] / (small.sum(2, keepdims=True) + 1))
            bh, bw = lens.changed.shape
            for x, y, w, h in detect.regions(lens.changed):
                found.append([x / bw, (i + y / bh) / LENSES, w / bw, h / bh / LENSES])
        rec = dict(time=when.isoformat() if when else None, regions=[[round(v, 4) for v in r] for r in found])
        if found:
            crops = [(x0, y0, img[y0:y0 + side, x0:x0 + side]) for x0, y0, side in detect.crops_for(found, W, H, LENSES)]
            rec["persons"] = [p for p in persons_in(det, img, crops)
                              if any(detect.overlaps(p["box"], r) for r in found)]
        if (start_index + k) % CHECK_EVERY == 0:
            rec["brute"] = persons_in(det, img, list(tiles(img)), 0.5)   # stricter: no motion to confirm
        if rec.get("persons") or (rec.get("brute") and not rec.get("persons")):
            rec["_img"] = img
        records.append(rec)
    return records


def draw(img, rec):
    H, W = img.shape[:2]
    out = img.copy()
    for x, y, w, h in rec["regions"]:
        cv2.rectangle(out, (int(x * W), int(y * H)), (int((x + w) * W), int((y + h) * H)), (0, 140, 255), 3)
    for key, colour in (("brute", (255, 180, 0)), ("persons", (60, 220, 60))):
        for p in rec.get(key) or []:
            x, y, w, h = p["box"]
            cv2.rectangle(out, (int(x * W), int(y * H)), (int((x + w) * W), int((y + h) * H)), colour, 5)
            cv2.putText(out, f"{'person' if key == 'persons' else 'brute'} {p['score']:.2f}",
                        (int(x * W), max(30, int(y * H) - 10)), cv2.FONT_HERSHEY_SIMPLEX, 1.4, colour, 3)
    cv2.putText(out, rec["time"] or "", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (255, 255, 255), 4)
    return cv2.resize(out, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


def main():
    begin, end = sys.argv[1], sys.argv[2]
    cid = sys.argv[3] if len(sys.argv) > 3 else "icsee_xm535"
    cfg = json.loads((ROOT / "cameras.json").read_text())[cid]
    rec_dir, out = ROOT / "recordings", ROOT / "recordings" / "scan" / cid
    (out / "files").mkdir(parents=True, exist_ok=True)
    (out / "snaps").mkdir(exist_ok=True)
    # daytime first (when people are likely), then nights; each in time order
    files = sorted(DVRIP(cfg["ip"], cfg["user"], cfg["password"]).files(begin, end),
                   key=lambda f: (not 7 <= int(f["BeginTime"][11:13]) < 21, f["BeginTime"]))
    print(f"{len(files)} files, {sum(int(f['FileLength'], 16) for f in files) / 1e6:.1f} GB", flush=True)
    jobs, done = queue.Queue(), {}
    for i, f in enumerate(files):
        jobs.put((i, f))
    for _ in range(WORKERS):
        threading.Thread(target=downloader, args=(cfg, jobs, done, rec_dir), daemon=True).start()

    det = detect.PersonDetector(str(ROOT / "models" / "yolox_s.onnx"))
    lenses = [detect.LensMotion() for _ in range(LENSES)]
    t0, index, prev_end = time.time(), 0, None
    for i, f in enumerate(files):
        if prev_end and f["BeginTime"] > prev_end:      # jumped ahead in time: start a fresh background
            lenses = [detect.LensMotion() for _ in range(LENSES)]
        prev_end = f["EndTime"]
        while i not in done:
            time.sleep(1)
        res_file = out / "files" / (local_name(f).name + ".json")
        if res_file.exists():
            records = json.loads(res_file.read_text())
            index += len(records)
            continue
        try:
            records = analyse(done[i], lenses, det, index)
        except Exception as e:  # noqa: BLE001
            print(f"  analysis failed {f['FileName']}: {e}", flush=True)
            continue
        index += len(records)
        for r in records:
            img = r.pop("_img", None)
            if img is not None:
                cv2.imwrite(str(out / "snaps" / (r["time"].replace(":", "") + ".jpg")), draw(img, r),
                            [cv2.IMWRITE_JPEG_QUALITY, 80])
        res_file.write_text(json.dumps(records))
        np_ = sum(1 for r in records if r.get("persons"))
        nm = sum(1 for r in records if r["regions"])
        nb = sum(1 for r in records if r.get("brute"))
        print(f"[{i + 1}/{len(files)}] {f['BeginTime']} keyframes={len(records)} motion={nm} person={np_} "
              f"brute_person={nb}  ({(time.time() - t0) / 60:.0f} min)", flush=True)
    summarise(out, files)


def summarise(out, files):
    """Group keyframes with people into events (gaps < EVENT_GAP s) and compare with brute force."""
    recs = []
    for f in files:
        p = out / "files" / (local_name(f).name + ".json")
        if p.exists():
            recs += [r for r in json.loads(p.read_text()) if r["time"]]
    recs.sort(key=lambda r: r["time"])
    events, cur = [], None
    for r in recs:
        t = datetime.fromisoformat(r["time"])
        if not r.get("persons"):
            # motion right after a sighting keeps the visit going (person hidden behind something)
            if r["regions"] and cur and t - cur["last"] <= timedelta(seconds=EVENT_GAP):
                cur["last"] = t
            continue
        if cur and t - cur["last"] <= timedelta(seconds=EVENT_GAP):
            cur["last"] = t
            cur["frames"] += 1
            cur["max_persons"] = max(cur["max_persons"], len(r["persons"]))
            best = max(p["score"] for p in r["persons"])
            if best > cur["best"]:
                cur.update(best=best, snapshot=r["time"].replace(":", "") + ".jpg")
        else:
            cur = dict(start=t, last=t, frames=1, max_persons=len(r["persons"]),
                       best=max(p["score"] for p in r["persons"]), snapshot=r["time"].replace(":", "") + ".jpg")
            events.append(cur)
    motion_only = sum(1 for r in recs if r["regions"] and not r.get("persons"))
    # brute-force cross-check: keyframes where a full-image search saw a person
    brute = [r for r in recs if r.get("brute")]
    hit = 0
    for r in brute:
        t = datetime.fromisoformat(r["time"])
        if any(e["start"] - timedelta(seconds=10) <= t <= e["last"] + timedelta(seconds=10) for e in events):
            hit += 1
    summary = dict(keyframes=len(recs), keyframes_with_motion=sum(1 for r in recs if r["regions"]),
                   keyframes_with_person=sum(1 for r in recs if r.get("persons")), motion_only_keyframes=motion_only,
                   brute_checks=sum(1 for r in recs if "brute" in r), brute_person_frames=len(brute),
                   brute_person_frames_inside_events=hit,
                   events=[dict(e, start=e["start"].isoformat(), last=e["last"].isoformat(),
                                minutes=round((e["last"] - e["start"]).total_seconds() / 60, 1)) for e in events])
    (out / "events.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "events"}, indent=1))
    for e in summary["events"]:
        print(f"  {e['start']} → {e['last'][11:]}  {e['minutes']:5.1f} min  up to {e['max_persons']} people  "
              f"best {e['best']:.2f}  {e['snapshot']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--summary":
        cid = sys.argv[2] if len(sys.argv) > 2 else "icsee_xm535"
        cfg = json.loads((ROOT / "cameras.json").read_text())[cid]
        out = ROOT / "recordings" / "scan" / cid   # summary of what has been analysed so far
        summarise(out, sorted(DVRIP(cfg["ip"], cfg["user"], cfg["password"]).files(
            "2026-11-14 00:00:00", "2026-11-15 23:59:59"), key=lambda f: f["BeginTime"]))
    else:
        main()
