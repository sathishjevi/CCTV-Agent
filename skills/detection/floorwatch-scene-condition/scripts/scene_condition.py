#!/usr/bin/env python3
"""
Floorwatch Scene-Condition Skill.

Reads `frame` JSONL events from stdin (same protocol every detection
skill in this repo consumes — see floorwatch-ingest/scripts/ingest.py),
and on a per-camera interval (NOT every frame — see --check-interval-
seconds; a vision-model call per frame at video framerate would be both
pointless for a condition that changes over minutes, not milliseconds,
and needlessly expensive) asks a configurable vision provider "does this
scene need staff attention" via vision_providers.py.

A positive judgment becomes a schema-compliant `scene_task_suggested`
event (skills/lib/floorwatch_schema.py) — validated the same way every
other event in this pipeline is, then emitted to stdout and (if
configured) published onto the same Redis Stream floorwatch-coverage
publishes zone_gap/zone_covered onto. The rules engine's own
_maybe_auto_assign() picks it up from there and routes it to the
department's primary-contact supervisor — this skill's only job is the
judgment call; everything downstream already exists.

Camera -> zone_id/role_tag mapping reuses floorwatch-coverage's own zone
calibration files (--zones-dir) rather than inventing a second config
format for the same information. A camera with more than one calibrated
zone uses the first — this skill judges the whole frame, not a polygon
sub-region, so multi-zone-per-camera setups get an approximation, not a
precise per-zone judgment.

Usage:
  python scene_condition.py --zones-dir zones --provider openai --api-key sk-... --model gpt-4o-mini
  python scene_condition.py --zones-dir zones --provider claude --api-key sk-ant-... --model claude-haiku-4-5 \\
      --redis-url redis://localhost:6379/0
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from vision_providers import build_vision_provider  # noqa: E402

_coverage_scripts_dir = _script_dir.parent.parent / "floorwatch-coverage" / "scripts"
sys.path.insert(0, str(_coverage_scripts_dir))
from zone_utils import load_all_zones  # noqa: E402 — reuse, don't fork a second zone-config format

_lib_candidates = [
    _script_dir,
    _script_dir.parent.parent.parent / "lib",
    _script_dir.parent / "lib",
]
_schema_loaded = False
for _lib_path in _lib_candidates:
    if (_lib_path / "floorwatch_schema.py").exists():
        sys.path.insert(0, str(_lib_path))
        from floorwatch_schema import validate_event  # noqa: E402
        _schema_loaded = True
        break
if not _schema_loaded:
    raise ImportError("floorwatch_schema.py not found — expected at skills/lib/floorwatch_schema.py")

SKILL_VERSION = "0.1.0"


def log(msg: str):
    print(f"[FLOORWATCH-SCENE-CONDITION] {msg}", file=sys.stderr, flush=True)


def emit(event: dict):
    print(json.dumps(event), flush=True)


def make_redis_publisher(redis_url: str, stream: str):
    import redis
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.ping()

    def publish(event: dict):
        client.xadd(stream, {"data": json.dumps(event)})

    return publish


def parse_args():
    parser = argparse.ArgumentParser(description="Floorwatch Scene-Condition Skill")
    parser.add_argument("--config", type=str, help="Path to config JSON file")
    parser.add_argument("--zones-dir", type=str, default="zones",
                         help="Reuses floorwatch-coverage's zone calibration files for camera -> zone_id/role_tag")
    parser.add_argument("--provider", type=str, default="none",
                         choices=["none", "openai", "grok", "llama", "claude", "anthropic"])
    parser.add_argument("--api-key", type=str, default="")
    parser.add_argument("--model", type=str, default="")
    parser.add_argument("--base-url", type=str, default=None,
                         help="Required for --provider llama (which host is serving the model); "
                              "optional override for openai/grok")
    parser.add_argument("--check-interval-seconds", type=float, default=900.0,
                         help="Minimum time between vision checks for the SAME camera (default 15 min)")
    parser.add_argument("--redis-url", type=str, default=None)
    parser.add_argument("--redis-stream", type=str, default="floorwatch:events")
    return parser.parse_args()


def load_config(args):
    """Same precedence as floorwatch-coverage: AEGIS_SKILL_PARAMS env var,
    then --config file, then individual FLOORWATCH_SCENE_* env vars
    (for deployment as its own Railway service, matching the
    FLOORWATCH_-prefixed convention floorwatch-rules-engine already
    uses — Aegis's skill-registry sets AEGIS_SKILL_PARAMS instead, a
    Railway deployment sets plain named env vars), then CLI args."""
    env_params = os.environ.get("AEGIS_SKILL_PARAMS")
    if env_params:
        try:
            return json.loads(env_params)
        except json.JSONDecodeError:
            pass

    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            with open(config_path) as f:
                return json.load(f)

    if os.environ.get("FLOORWATCH_SCENE_PROVIDER"):
        return {
            "zones_dir": os.environ.get("FLOORWATCH_SCENE_ZONES_DIR", args.zones_dir),
            "provider": os.environ.get("FLOORWATCH_SCENE_PROVIDER", args.provider),
            "api_key": os.environ.get("FLOORWATCH_SCENE_API_KEY", args.api_key),
            "model": os.environ.get("FLOORWATCH_SCENE_MODEL", args.model),
            "base_url": os.environ.get("FLOORWATCH_SCENE_BASE_URL") or args.base_url,
            "check_interval_seconds": float(os.environ.get(
                "FLOORWATCH_SCENE_CHECK_INTERVAL_SECONDS", args.check_interval_seconds)),
            "redis_url": os.environ.get("FLOORWATCH_SCENE_REDIS_URL", args.redis_url),
            "redis_stream": os.environ.get("FLOORWATCH_SCENE_REDIS_STREAM", args.redis_stream),
        }

    return {
        "zones_dir": args.zones_dir,
        "provider": args.provider,
        "api_key": args.api_key,
        "model": args.model,
        "base_url": args.base_url,
        "check_interval_seconds": args.check_interval_seconds,
        "redis_url": args.redis_url,
        "redis_stream": args.redis_stream,
    }


def build_camera_zone_map(zones_dir: Path) -> dict:
    """{camera_id: (zone_id, role_tag)} — first calibrated zone per camera."""
    zones_by_camera = load_all_zones(zones_dir)
    result = {}
    for camera_id, zones in zones_by_camera.items():
        if zones:
            result[camera_id] = (zones[0].zone_id, zones[0].role_tag)
    return result


def judgment_to_event(judgment, camera_id: str, zone_id: str, role_tag: str, timestamp: str) -> dict:
    raw_event = {
        "camera_id": camera_id, "zone_id": zone_id, "role_tag": role_tag,
        "timestamp": timestamp or None, "entity_ref": None,
        "event_type": "scene_task_suggested",
        "task_name": judgment.task_name, "task_type": judgment.task_type,
        "message": judgment.message,
        "confidence": round(judgment.confidence, 3),
        "source_model_version": f"floorwatch-scene-condition/{SKILL_VERSION}",
    }
    if not raw_event["timestamp"]:
        del raw_event["timestamp"]
    return raw_event


def main():
    args = parse_args()
    config = load_config(args)

    zones_dir_raw = config.get("zones_dir", "zones")
    zones_dir = Path(zones_dir_raw)
    if not zones_dir.is_absolute():
        zones_dir = _script_dir.parent / zones_dir_raw
    camera_zone_map = build_camera_zone_map(zones_dir)
    if not camera_zone_map:
        log(f"WARNING: no zone calibration found in {zones_dir} — every frame will be skipped "
            f"until at least one camera has a calibrated zone (reuses floorwatch-coverage's zones/)")

    check_interval = config.get("check_interval_seconds", 900.0)
    redis_url = config.get("redis_url")
    redis_stream = config.get("redis_stream", "floorwatch:events")

    provider = build_vision_provider(
        config.get("provider", "none"), config.get("api_key", ""),
        config.get("model", ""), config.get("base_url"))

    publish = None
    if redis_url:
        try:
            publish = make_redis_publisher(redis_url, redis_stream)
            log(f"Publishing events to Redis stream '{redis_stream}' at {redis_url}")
        except Exception as e:
            log(f"WARNING: could not connect to Redis at {redis_url}: {e} — continuing stdout-only")

    emit({
        "event": "ready",
        "skill": "floorwatch-scene-condition",
        "version": SKILL_VERSION,
        "provider": config.get("provider", "none"),
        "provider_active": provider is not None,
        "cameras_calibrated": list(camera_zone_map.keys()),
        "check_interval_seconds": check_interval,
        "redis_enabled": publish is not None,
    })
    if provider is None:
        log("No vision provider configured/available — skill will read frames but never judge them "
            "(set --provider/--api-key/--model, or AEGIS_SKILL_PARAMS equivalent)")

    def handle_signal(signum, frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log(f"Received {sig_name}, shutting down gracefully")
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_checked_at: dict = {}  # camera_id -> monotonic time of last vision check

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("command") == "stop":
            break
        if msg.get("event") != "frame" or provider is None:
            continue

        camera_id = msg.get("camera_id")
        zone_info = camera_zone_map.get(camera_id)
        if zone_info is None:
            continue  # uncalibrated camera — no zone_id/role_tag to attach to an event

        now = time.monotonic()
        if now - last_checked_at.get(camera_id, -1e18) < check_interval:
            continue  # too soon since this camera's last check — cost control
        last_checked_at[camera_id] = now

        frame_path = msg.get("frame_path")
        if not frame_path:
            continue

        try:
            judgment = provider.judge(Path(frame_path))
        except Exception as e:
            log(f"unexpected error judging frame for camera '{camera_id}': {e}")
            continue
        if judgment is None or not judgment.needs_task:
            continue

        zone_id, role_tag = zone_info
        raw_event = judgment_to_event(judgment, camera_id, zone_id, role_tag, msg.get("timestamp", ""))
        validated = validate_event(raw_event, log=log)
        if validated is None:
            continue
        evt = validated.model_dump()
        emit(evt)
        if publish:
            publish(evt)


if __name__ == "__main__":
    main()
