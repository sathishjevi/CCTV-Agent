#!/usr/bin/env bash
# entrypoint.sh — materializes cameras.json from an env var (since it's
# site-specific and never baked into the image, see Dockerfile), then
# builds and execs the tools/run_pipeline.py command from env vars so no
# Railway "Custom Start Command" is needed.
set -euo pipefail

LOG_PREFIX="[floorwatch-pipeline]"
log() { echo "$LOG_PREFIX $*" >&2; }

CAMERAS_PATH="${FLOORWATCH_CAMERAS_PATH:-/data/cameras.json}"
mkdir -p "$(dirname "$CAMERAS_PATH")"

if [ -n "${FLOORWATCH_CAMERAS_JSON:-}" ]; then
    log "Writing cameras.json from FLOORWATCH_CAMERAS_JSON to $CAMERAS_PATH"
    printf '%s' "$FLOORWATCH_CAMERAS_JSON" > "$CAMERAS_PATH"
elif [ -f "$CAMERAS_PATH" ]; then
    log "Using existing cameras.json already present at $CAMERAS_PATH (mounted volume?)"
else
    log "FATAL: no cameras.json available."
    log "Set FLOORWATCH_CAMERAS_JSON as a Railway variable, containing the raw JSON"
    log "content of your cameras.json (see cameras.json.template) — remember only"
    log "s3/azure_blob/gcs/http_api/ezviz source types work from this container;"
    log "rtsp/local_folder cameras need a machine with actual network/filesystem"
    log "access to them instead (see CCTV_INTEGRATION_SETUP.md)."
    exit 1
fi

if ! python -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$CAMERAS_PATH"; then
    log "FATAL: $CAMERAS_PATH is not valid JSON — check FLOORWATCH_CAMERAS_JSON for a copy/paste error."
    exit 1
fi

ARGS=(--cameras "$CAMERAS_PATH")
[ -n "${FLOORWATCH_REDIS_URL:-}" ] && ARGS+=(--redis-url "$FLOORWATCH_REDIS_URL")
[ -n "${FLOORWATCH_REDIS_STREAM:-}" ] && ARGS+=(--events-stream "$FLOORWATCH_REDIS_STREAM")
[ -n "${FLOORWATCH_REDIS_MOTION_STREAM:-}" ] && ARGS+=(--motion-stream "$FLOORWATCH_REDIS_MOTION_STREAM")
[ -n "${FLOORWATCH_ZONES_DIR:-}" ] && ARGS+=(--zones-dir "$FLOORWATCH_ZONES_DIR")
[ -n "${FLOORWATCH_MODEL_SIZE:-}" ] && ARGS+=(--model-size "$FLOORWATCH_MODEL_SIZE")
[ -n "${FLOORWATCH_DETECT_CONFIDENCE:-}" ] && ARGS+=(--confidence "$FLOORWATCH_DETECT_CONFIDENCE")
[ -n "${FLOORWATCH_DETECT_DEVICE:-}" ] && ARGS+=(--device "$FLOORWATCH_DETECT_DEVICE")
[ -n "${FLOORWATCH_DETECT_FPS:-}" ] && ARGS+=(--detect-fps "$FLOORWATCH_DETECT_FPS")
[ -n "${FLOORWATCH_POSE_FPS:-}" ] && ARGS+=(--pose-fps "$FLOORWATCH_POSE_FPS")
[ "${FLOORWATCH_SKIP_POSE:-false}" = "true" ] && ARGS+=(--skip-pose)

log "Starting: python tools/run_pipeline.py ${ARGS[*]}"
exec python tools/run_pipeline.py "${ARGS[@]}"
