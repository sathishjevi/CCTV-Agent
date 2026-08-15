#!/usr/bin/env bash
# deploy.sh — Bootstrapper for Floorwatch Ingest Skill.
#
# Installs core dependencies always; prompts about optional cloud SDKs
# based on what's actually configured in cameras.json, so a deployment
# using only RTSP doesn't get boto3/azure/google-cloud-storage installed
# for no reason.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_PREFIX="[FLOORWATCH-INGEST-deploy]"

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

emit '{"event":"progress","stage":"deps","message":"Installing core dependencies (opencv, numpy, httpx)..."}'
"$PYTHON_BIN" -m pip install -q -r "$SKILL_DIR/requirements.txt"

CAMERAS_FILE="$SKILL_DIR/cameras.json"
if [ -f "$CAMERAS_FILE" ]; then
    if grep -q '"source_type"[[:space:]]*:[[:space:]]*"s3"' "$CAMERAS_FILE"; then
        emit '{"event":"progress","stage":"deps","message":"Installing boto3 (S3 configured)..."}'
        "$PYTHON_BIN" -m pip install -q boto3
    fi
    if grep -qE '"source_type"[[:space:]]*:[[:space:]]*"azure' "$CAMERAS_FILE"; then
        emit '{"event":"progress","stage":"deps","message":"Installing azure-storage-blob (Azure configured)..."}'
        "$PYTHON_BIN" -m pip install -q azure-storage-blob
    fi
    if grep -qE '"source_type"[[:space:]]*:[[:space:]]*"gcs"' "$CAMERAS_FILE"; then
        emit '{"event":"progress","stage":"deps","message":"Installing google-cloud-storage (GCS configured)..."}'
        "$PYTHON_BIN" -m pip install -q google-cloud-storage
    fi
    if grep -qE '"source_type"[[:space:]]*:[[:space:]]*"ezviz"' "$CAMERAS_FILE"; then
        emit '{"event":"progress","stage":"deps","message":"Installing pyezvizapi (EZVIZ configured — see sources/ezviz.py's module docstring for the ToS/security tradeoffs of this integration)..."}'
        "$PYTHON_BIN" -m pip install -q pyezvizapi
    fi
else
    log "No cameras.json found yet — skipping optional cloud SDK install. Copy cameras.json.template and re-run once configured."
fi

emit '{"event":"complete","backend":"cpu","message":"Installed!"}'
log "Deployment complete."
