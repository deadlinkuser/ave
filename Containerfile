FROM docker.io/library/python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AVE_DATA_DIR=/data \
    AVE_PORT=8488

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    util-linux \
    procps \
    vainfo \
    mesa-va-drivers \
    intel-media-va-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ave/ ./ave/
COPY entrypoint.py .

RUN mkdir -p /data/watch/_originals /data/watch/_failed /data/output /data/config

EXPOSE 8488
ENTRYPOINT ["python3", "/app/entrypoint.py"]
