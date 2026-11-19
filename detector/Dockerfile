FROM python:3.12-slim
RUN pip install --no-cache-dir opencv-python-headless==4.12.0.88 numpy==2.2.6
WORKDIR /app
COPY detect.py .
ENV PYTHONUNBUFFERED=1 OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"
EXPOSE 8000
CMD ["python", "detect.py"]
