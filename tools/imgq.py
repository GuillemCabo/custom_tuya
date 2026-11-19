"""Image-quality measures for camera tuning (no reference image needed).

score() combines BRISQUE (a standard no-reference quality model: noise, blur, compression and
unnatural statistics all raise it; lower is better) with penalties for clipped highlights/shadows.
Each lens of a dual-lens frame is scored separately and averaged.
"""
import urllib.request
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(__file__).resolve().parent.parent / "models"
_brisque = None


def brisque(gray):
    global _brisque
    if _brisque is None:
        _brisque = cv2.quality.QualityBRISQUE_create(str(MODELS / "brisque_model_live.yml"),
                                                     str(MODELS / "brisque_range_live.yml"))
    return float(_brisque.compute(gray)[0])


def noise_sigma(gray):
    """Immerkaer's fast noise estimate (std of the noise in grey levels)."""
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    h, w = gray.shape
    conv = cv2.filter2D(gray.astype(np.float32), -1, k)
    return float(np.sum(np.abs(conv)) * np.sqrt(0.5 * np.pi) / (6 * (w - 2) * (h - 2)))


def measures(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    hist = np.bincount(gray.ravel(), minlength=256) / gray.size
    nz = hist[hist > 0]
    b, g, r = [c.astype(np.float32) for c in cv2.split(bgr)]
    rg, yb = r - g, 0.5 * (r + g) - b
    return dict(
        brisque=brisque(small),
        clip_hi=float(hist[250:].sum()), clip_lo=float(hist[:6].sum()),
        mean=float(gray.mean()), contrast=float(gray.std()), entropy=float(-(nz * np.log2(nz)).sum()),
        detail=float(cv2.Laplacian(small, cv2.CV_32F).var()), noise=noise_sigma(small),
        colour=float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)))


def score(m):
    """Lower is better."""
    return m["brisque"] + 120 * m["clip_hi"] + 60 * m["clip_lo"]


def lenses(bgr):
    h = bgr.shape[0] // 2
    return [bgr[:h], bgr[h:]]


def evaluate(bgr):
    per = [measures(x) for x in lenses(bgr)]
    avg = {k: float(np.mean([p[k] for p in per])) for k in per[0]}
    avg["score"] = float(np.mean([score(p) for p in per]))
    return avg


def grab(go2rtc, stream, timeout=20):
    with urllib.request.urlopen(f"{go2rtc}/api/frame.jpeg?src={stream}", timeout=timeout) as r:
        data = np.frombuffer(r.read(), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)
