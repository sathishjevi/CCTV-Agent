#!/usr/bin/env bash
# deploy.sh — Bootstrapper for Floorwatch Pose Skill.
#
# Installs mediapipe/pillow/numpy/redis, then attempts to download the
# MediaPipe PoseLandmarker model bundle. If the download fails (restricted
# network, no internet), the skill still installs successfully and runs
# in fallback (frame-differencing) mode automatically — see SKILL.md.
#
# Exit codes:
#   0 = success (real or fallback mode)
#   1 = fatal error (no Python, or pip install failed)

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="$SKILL_DIR/models"
MODEL_PATH="$MODEL_DIR/pose_landmarker_lite.task"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
LOG_PREFIX="[FLOORWATCH-POSE-deploy]"

log()  { echo "$LOG_PREFIX $*" >&2; }
emit() { echo "$1"; }

find_python() {
    for cmd in python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver major minor
            ver="$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')"
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

emit '{"event":"progress","stage":"python","message":"Locating Python interpreter..."}'
PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
    emit '{"event":"error","message":"No Python >=3.9 interpreter found","retriable":false}'
    log "FATAL: no suitable Python interpreter found"
    exit 1
fi
log "Using $PYTHON_BIN"

emit '{"event":"progress","stage":"deps","message":"Installing mediapipe/pillow/numpy/redis..."}'
if ! "$PYTHON_BIN" -m pip install -q -r "$SKILL_DIR/requirements.txt"; then
    emit '{"event":"error","message":"pip install failed","retriable":true}'
    log "FATAL: dependency install failed"
    exit 1
fi

mkdir -p "$MODEL_DIR"
if [ -f "$MODEL_PATH" ]; then
    log "Pose model already present at $MODEL_PATH"
else
    emit '{"event":"progress","stage":"model","message":"Downloading MediaPipe pose model..."}'
    if curl -fsSL -o "$MODEL_PATH" "$MODEL_URL" 2>/dev/null || wget -q -O "$MODEL_PATH" "$MODEL_URL" 2>/dev/null; then
        log "Downloaded pose model to $MODEL_PATH"
    else
        rm -f "$MODEL_PATH"
        log "WARNING: could not download pose model from $MODEL_URL (network/firewall?)."
        log "WARNING: floorwatch-pose will run in FALLBACK (frame-differencing) mode until a"
        log "WARNING: model file is placed at $MODEL_PATH — see SKILL.md 'Two execution modes'."
        emit '{"event":"progress","stage":"model","message":"Model download failed — will run in fallback mode"}'
    fi
fi

emit '{"event":"complete","backend":"cpu","message":"Installed!"}'
log "Deployment complete."
