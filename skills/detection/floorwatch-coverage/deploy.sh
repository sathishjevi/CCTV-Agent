#!/usr/bin/env bash
# deploy.sh — Bootstrapper for Floorwatch Coverage Skill.
#
# This skill runs no ML model of its own (it only consumes another
# skill's detection output), but it is NOT dependency-free: zone
# membership is tested with supervision.PolygonZone, so requirements.txt
# must actually be installed before the import check below can pass.
# Mirrors the JSONL progress protocol used by
# skills/detection/yolo-detection-2026/deploy.sh so Aegis's installer UI
# works the same way for both skills.
#
# Python floor is 3.10, not 3.9 — that's supervision's own Requires-Python.
#
# Exit codes:
#   0 = success
#   1 = fatal error (no suitable Python, or dependency install failed)

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_PREFIX="[FLOORWATCH-COVERAGE-deploy]"

log()  { echo "$LOG_PREFIX $*" >&2; }
emit() { echo "$1"; }

find_python() {
    # `python` is included (last, so a versioned binary still wins) because on
    # Windows/Git-Bash `python3` is often the Microsoft Store stub: command -v
    # finds it, but `--version` prints an install prompt with no version number
    # while the real interpreter is plain `python`.
    for cmd in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver major minor
            ver="$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)"
            # An unparseable version means this candidate isn't a real
            # interpreter (see the Store-stub case above) — skip it rather
            # than feeding "" into the integer comparisons below, which
            # aborts the whole script under `set -e`.
            [ -z "$ver" ] && continue
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
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
    emit '{"event":"error","message":"No Python >=3.10 interpreter found","retriable":false}'
    log "FATAL: no suitable Python interpreter found"
    exit 1
fi
log "Using $PYTHON_BIN"

emit '{"event":"progress","stage":"deps","message":"Installing Python dependencies..."}'
REQ_FILE="$SKILL_DIR/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
    emit '{"event":"error","message":"requirements.txt not found next to deploy.sh","retriable":false}'
    log "FATAL: $REQ_FILE not found"
    exit 1
fi
if ! "$PYTHON_BIN" -m pip install -r "$REQ_FILE" -q 2>&1 | tail -5 >&2; then
    emit '{"event":"error","message":"Dependency install failed","retriable":true}'
    log "FATAL: pip install -r requirements.txt failed"
    exit 1
fi

# Import check runs AFTER the install above — zone_utils imports numpy and
# supervision at module level, so on a clean machine this check is exactly
# what catches a dependency install that silently didn't take. stderr is
# surfaced (not sent to /dev/null) so the real ImportError is diagnosable.
emit '{"event":"progress","stage":"verify","message":"Verifying skill modules import..."}'
# cd into scripts/ and import from the CWD rather than injecting an absolute
# path into sys.path: `python -c` already puts the CWD on sys.path, and this
# avoids handing a POSIX path ("/c/Product/...") to a native Windows
# interpreter under Git Bash, which cannot resolve it.
if ! (cd "$SKILL_DIR/scripts" && "$PYTHON_BIN" -c "import zone_utils") 2>&1 | tail -5 >&2; then
    emit '{"event":"error","message":"Skill module import check failed","retriable":true}'
    log "FATAL: zone_utils.py failed to import"
    exit 1
fi

mkdir -p "$SKILL_DIR/zones"

emit '{"event":"complete","backend":"cpu","message":"Installed!"}'
log "Deployment complete."
