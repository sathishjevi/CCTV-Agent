#!/usr/bin/env python3
"""
Floorwatch Coverage Skill — Phase 2.

Downstream consumer of a detection skill's output (e.g. yolo-detection-2026).
Reads `detections` JSONL from stdin, maps person bboxes into calibrated zone
polygons, debounces per-zone occupancy (60s / >=0.8 confidence per the
brief), and emits schema-compliant `zone_covered` / `zone_gap` events —
validated against the shared Floorwatch event schema before being emitted
to stdout and (optionally) published onto a Redis Stream event bus.

No video/frames are read or stored here — only structured detection JSON
in, structured schema-compliant JSON out.

Usage:
  python main.py --zones-dir zones
  python main.py --zones-dir zones --redis-url redis://localhost:6379/0
  echo '{"event":"detections",...}' | python main.py --zones-dir zones
"""

import sys
import os
import json
import argparse
import signal
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
import numpy as np
import supervision as sv

from zone_utils import load_all_zones, build_polygon_zone  # noqa: E402
from debounce import DebouncerRegistry  # noqa: E402

# Locate skills/lib/floorwatch_schema.py — same multi-candidate pattern
# yolo-detection-2026/scripts/detect.py uses for env_config.py.
_lib_candidates = [
    _script_dir,
    _script_dir.parent.parent.parent / "lib",  # repo: skills/lib/
    _script_dir.parent / "lib",                 # skill-level lib/
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

SKILL_VERSION = "0.2.0"


def parse_args():
    parser = argparse.ArgumentParser(description="Floorwatch Coverage Skill")
    parser.add_argument("--config", type=str, help="Path to config JSON file")
    parser.add_argument("--zones-dir", type=str, default="zones")
    parser.add_argument("--person-class", type=str, default="person")
    parser.add_argument("--min-confidence", type=float, default=0.5,
                        help="Confidence floor for a person detection to count toward occupancy at all")
    parser.add_argument("--gap-confidence-threshold", type=float, default=0.8,
                        help="Confidence a person detection must clear to count as 'covered' for debounce purposes")
    parser.add_argument("--debounce-seconds", type=float, default=60.0)
    parser.add_argument("--anchor-point", type=str, default="bottom_center",
                        choices=["bottom_center", "center"])
    parser.add_argument("--redis-url", type=str, default=None,
                        help="If set, publish events onto this Redis Stream in addition to stdout")
    parser.add_argument("--redis-stream", type=str, default="floorwatch:events")
    parser.add_argument("--shadow-mode", action="store_true", default=True,
                        help="Log-only mode (default). Notifications are a downstream rules-engine concern; "
                             "this flag is threaded through as a passthrough field on emitted events.")
    return parser.parse_args()


def load_config(args):
    """Load config from AEGIS_SKILL_PARAMS env var, then --config file, then CLI args."""
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

    return {
        "zones_dir": args.zones_dir,
        "person_class": args.person_class,
        "min_confidence": args.min_confidence,
        "gap_confidence_threshold": args.gap_confidence_threshold,
        "debounce_seconds": args.debounce_seconds,
        "anchor_point": args.anchor_point,
        "redis_url": args.redis_url,
        "redis_stream": args.redis_stream,
        "shadow_mode": args.shadow_mode,
    }


def emit(event: dict):
    print(json.dumps(event), flush=True)


def log(msg: str):
    print(f"[FLOORWATCH-COVERAGE] {msg}", file=sys.stderr, flush=True)


def make_redis_publisher(redis_url: str, stream: str):
    import redis
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.ping()

    def publish(event: dict):
        client.xadd(stream, {"data": json.dumps(event)})

    return publish


# One sv.PolygonZone per (zone_id, polygon, anchor) — safe and worthwhile to
# reuse across every frame rather than rebuild per call (see build_polygon_zone()'s
# docstring for why reuse is correct, not just an optimization).
_polygon_zone_cache: dict = {}


def _get_polygon_zone(zone, anchor: str):
    key = (zone.zone_id, tuple(tuple(p) for p in zone.polygon), anchor)
    cached = _polygon_zone_cache.get(key)
    if cached is None:
        cached = build_polygon_zone(zone, anchor)
        _polygon_zone_cache[key] = cached
    return cached


def compute_zone_occupancy(msg: dict, zones_by_camera: dict, person_class: str, min_confidence: float,
                            anchor: str = "bottom_center"):
    """Reduce one detections event into per-zone (occupied, confidence, entity_ref) observations.

    Zone-membership testing is sv.PolygonZone (see zone_utils.build_polygon_zone) —
    each zone is tested independently against the same detections batch, but a
    person is only ever counted toward the FIRST zone (in zones' list order) that
    contains them, matching this function's original point-in-polygon behavior
    (relevant if calibrated zones ever overlap)."""
    camera_id = msg.get("camera_id", "unknown")
    timestamp = msg.get("timestamp", "")
    objects = msg.get("objects", [])

    zones = zones_by_camera.get(camera_id, [])
    if not zones:
        return []

    persons = [
        o for o in objects
        if o.get("class") == person_class and o.get("confidence", 0) >= min_confidence
    ]

    occupants_by_zone = {z.zone_id: [] for z in zones}
    if persons:
        xyxy = np.array([p["bbox"] for p in persons], dtype=np.float64)
        confidences = [p.get("confidence", 0.0) for p in persons]
        detections = sv.Detections(xyxy=xyxy)
        masks = [_get_polygon_zone(zone, anchor).trigger(detections) for zone in zones]

        for idx in range(len(persons)):
            entity_ref = f"track_{idx}"
            for mask, zone in zip(masks, zones):
                if mask[idx]:
                    occupants_by_zone[zone.zone_id].append((entity_ref, confidences[idx]))
                    break

    observations = []
    for zone in zones:
        occupants = occupants_by_zone[zone.zone_id]
        occupied = len(occupants) > 0
        top_entity, top_confidence = max(occupants, key=lambda o: o[1]) if occupants else (None, 0.0)
        observations.append({
            "camera_id": camera_id,
            "zone_id": zone.zone_id,
            "role_tag": zone.role_tag,
            "timestamp": timestamp,
            "occupied": occupied,
            "entity_ref": top_entity,
            "confidence": top_confidence,
        })
    return observations


def process_detections(msg: dict, zones_by_camera: dict, registry: DebouncerRegistry,
                        person_class: str, min_confidence: float, anchor: str = "bottom_center"):
    """Map one detections event into zero or more schema-validated zone_covered/zone_gap events."""
    events = []
    for obs in compute_zone_occupancy(msg, zones_by_camera, person_class, min_confidence, anchor):
        debouncer = registry.get(obs["camera_id"], obs["zone_id"])
        transition = debouncer.update(obs["occupied"], obs["confidence"], obs["entity_ref"], obs["timestamp"])
        if transition is None:
            continue

        raw_event = {
            "camera_id": obs["camera_id"],
            "zone_id": obs["zone_id"],
            "role_tag": obs["role_tag"],
            "timestamp": obs["timestamp"] or None,
            "entity_ref": transition["entity_ref"],
            "event_type": transition["event_type"],
            "confidence": round(transition["confidence"], 3),
            "source_model_version": f"floorwatch-coverage/{SKILL_VERSION}",
        }
        # timestamp is a required non-null field in the shared schema; fall back if upstream omitted it
        if not raw_event["timestamp"]:
            del raw_event["timestamp"]

        validated = validate_event(raw_event, log=log)
        if validated is not None:
            events.append(validated.model_dump())
    return events


def main():
    args = parse_args()
    config = load_config(args)

    zones_dir_raw = config.get("zones_dir", "zones")
    zones_dir = Path(zones_dir_raw)
    if not zones_dir.is_absolute():
        zones_dir = _script_dir.parent / zones_dir_raw
    person_class = config.get("person_class", "person")
    min_confidence = config.get("min_confidence", 0.5)
    anchor_point = config.get("anchor_point", "bottom_center")
    gap_confidence_threshold = config.get("gap_confidence_threshold", 0.8)
    debounce_seconds = config.get("debounce_seconds", 60.0)
    shadow_mode = config.get("shadow_mode", True)
    redis_url = config.get("redis_url")
    redis_stream = config.get("redis_stream", "floorwatch:events")

    zones_by_camera = load_all_zones(zones_dir)
    total_zones = sum(len(v) for v in zones_by_camera.values())
    if total_zones == 0:
        log(f"WARNING: no zone calibration files found in {zones_dir} — "
            f"no zone_covered/zone_gap output will be emitted until zones are calibrated")

    registry = DebouncerRegistry(debounce_seconds=debounce_seconds, min_confidence=gap_confidence_threshold)

    publish = None
    if redis_url:
        try:
            publish = make_redis_publisher(redis_url, redis_stream)
            log(f"Publishing events to Redis stream '{redis_stream}' at {redis_url}")
        except Exception as e:
            log(f"WARNING: could not connect to Redis at {redis_url}: {e} — continuing stdout-only")

    emit({
        "event": "ready",
        "skill": "floorwatch-coverage",
        "version": SKILL_VERSION,
        "cameras_calibrated": list(zones_by_camera.keys()),
        "zone_count": total_zones,
        "debounce_seconds": debounce_seconds,
        "gap_confidence_threshold": gap_confidence_threshold,
        "shadow_mode": shadow_mode,
        "redis_enabled": publish is not None,
    })

    def handle_signal(signum, frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log(f"Received {sig_name}, shutting down gracefully")
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

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

        if msg.get("event") == "detections":
            try:
                for evt in process_detections(msg, zones_by_camera, registry, person_class, min_confidence,
                                               anchor_point):
                    emit(evt)
                    if publish:
                        publish(evt)
            except Exception as e:
                emit({
                    "event": "error",
                    "message": f"Zone-mapping error: {e}",
                    "retriable": True,
                })


if __name__ == "__main__":
    main()
