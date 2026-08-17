# Floorwatch Pipeline — Railway deployment image for the ingestion→
# detection→coverage/pose→Redis pipeline (tools/run_pipeline.py).
#
# Build context MUST be the repo root — same reasoning as the other two
# Floorwatch Dockerfiles (see services/floorwatch-rules-engine/Dockerfile's
# header and RAILWAY_DEPLOYMENT.md).
#
# IMPORTANT — only cloud-reachable source types work here: this container
# has no access to a client's local camera LAN or local filesystem, so
# only cameras.json entries using source_type "s3", "azure_blob", "gcs",
# "http_api", or "ezviz" work when deployed this way. "rtsp" and
# "local_folder" cameras need a machine with actual network/filesystem
# access to that camera — run those on-prem instead (see
# CCTV_INTEGRATION_SETUP.md). Nothing stops you from mixing both in one
# cameras.json; the on-prem cameras just need a *separate* ingest.py
# process running somewhere that can reach them, publishing to the same
# Redis instance this container uses.
#
# NOT BUILD-TESTED against a real Docker daemon in this environment (see
# RAILWAY_DEPLOYMENT.md's own note on this) — verify with a real deploy.
FROM python:3.14-slim

WORKDIR /app

# System libs opencv-python-headless and mediapipe both need on a minimal
# Debian base (neither ships these itself) — this is the one meaningful
# addition versus the other two services' Dockerfiles, which don't touch
# any image/video decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching). opencv-python-headless is
# installed explicitly and once, before any requirements.txt that lists
# it — floorwatch-ingest's own requirements.txt asks for plain
# "opencv-python" (which needs GUI libs this headless image doesn't have
# and shouldn't need), so its dependency line is intentionally NOT
# pip-installed wholesale below; its other two deps (numpy, httpx) are
# listed explicitly instead.
COPY skills/detection/yolo-detection-2026/requirements.txt ./req/yolo-detection-2026.txt
COPY skills/detection/floorwatch-coverage/requirements.txt ./req/floorwatch-coverage.txt
COPY skills/detection/floorwatch-pose/requirements.txt ./req/floorwatch-pose.txt

RUN pip install --no-cache-dir \
        opencv-python-headless numpy httpx \
        onnxruntime \
    && pip install --no-cache-dir -r req/yolo-detection-2026.txt \
    && pip install --no-cache-dir -r req/floorwatch-coverage.txt \
    && pip install --no-cache-dir -r req/floorwatch-pose.txt \
    && pip install --no-cache-dir boto3 azure-storage-blob google-cloud-storage pyezvizapi

# Shared modules every skill's config.py / env_config.py imports from.
COPY skills/lib/ ./skills/lib/
COPY config/ ./config/

# The four skills tools/run_pipeline.py orchestrates, plus the
# orchestrator itself. Nothing from services/ is needed here — this
# container talks to floorwatch-rules-engine only via Redis.
COPY skills/detection/floorwatch-ingest/ ./skills/detection/floorwatch-ingest/
COPY skills/detection/yolo-detection-2026/ ./skills/detection/yolo-detection-2026/
COPY skills/detection/floorwatch-coverage/ ./skills/detection/floorwatch-coverage/
COPY skills/detection/floorwatch-pose/ ./skills/detection/floorwatch-pose/
COPY tools/run_pipeline.py ./tools/run_pipeline.py

COPY services/floorwatch-pipeline/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# cameras.json is site-specific and gitignored — never baked into the
# image (see cameras.json.template). entrypoint.sh materializes it at
# container start, either from FLOORWATCH_CAMERAS_JSON (its raw JSON
# content as one Railway variable) or from a file already present at
# FLOORWATCH_CAMERAS_PATH if you've mounted a Railway volume there.
ENV FLOORWATCH_CAMERAS_PATH=/data/cameras.json

CMD ["./entrypoint.sh"]
