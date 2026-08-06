#!/usr/bin/env bash
# deploy.sh — Bootstrapper for Floorwatch Coverage Skill.
#
# This skill has no ML/GPU dependencies (pure-Python zone geometry), so
# deployment only needs to locate a Python >=3.9 interpreter and verify
# the skill's own modules import cleanly. Mirrors the JSONL progress
# protocol used by skills/detection/yolo-detection-2026/deploy.sh so
# Aegis's installer UI works the same way for both skills.
#
# Exit codes:
#   0 = success
#   1 = fatal error (no Python found)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_PREFIX="[FLOORWATCH-COVERAGE-deploy]"

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

emit '{"event":"progress","stage":"verify","message":"Verifying skill modules import..."}'
if ! "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$SKILL_DIR/scripts'); import zone_utils" 2>/dev/null; then
    emit '{"event":"error","message":"Skill module import check failed","retriable":true}'
    log "FATAL: zone_utils.py failed to import"
    exit 1
fi

mkdir -p "$SKILL_DIR/zones"

emit '{"event":"complete","backend":"cpu","message":"Installed!"}'
log "Deployment complete."
