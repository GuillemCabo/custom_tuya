#!/bin/sh
# Downloads the models used by the detector and the image tuner into models/ (gitignored).
set -e
cd "$(dirname "$0")/../models" 2>/dev/null || { mkdir -p "$(dirname "$0")/../models"; cd "$(dirname "$0")/../models"; }
# YOLOX-S, COCO, Apache-2.0 — OpenCV model zoo build
curl -fL -o yolox_s.onnx https://huggingface.co/opencv/object_detection_yolox/resolve/main/object_detection_yolox_2022nov.onnx
echo "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063  yolox_s.onnx" | sha256sum -c
# BRISQUE image-quality model (opencv_contrib samples)
for f in brisque_model_live.yml brisque_range_live.yml; do
  curl -fL -o "$f" "https://raw.githubusercontent.com/opencv/opencv_contrib/4.x/modules/quality/samples/$f"
done
