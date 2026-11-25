# [NOTE] Rootless Podman Containerfile bundling Python, ffmpeg/ffprobe, ionice, and the AVE Web GUI.

FROM docker.io/library/python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AVE_DATA_DIR=/data \
    AVE_HOST=0.0.0.0 \
    AVE_PORT=8488

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ave/ ./ave/
COPY entrypoint.py .

VOLUME ["/data"]
EXPOSE 8488

CMD ["python3", "entrypoint.py"]
