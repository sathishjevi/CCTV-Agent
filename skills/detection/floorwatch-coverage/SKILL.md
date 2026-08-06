---
name: floorwatch-coverage
description: "Floorwatch Coverage — maps person detections into calibrated work zones and emits schema-compliant zone_covered/zone_gap events"
version: 0.2.0
icon: assets/icon.png
entry: scripts/main.py
deploy: deploy.sh

requirements:
  python: ">=3.9"
  platforms: ["linux", "macos", "windows"]

parameters:
  - name: auto_start
    label: "Auto Start"
    type: boolean
    default: false
    description: "Start this skill automatically when Aegis launches"
    group: Lifecycle

  - name: zones_dir
    label: "Zones Directory"
    type: string
    default: "zones"
    description: "Directory (relative to this skill) containing <camera_id>.json zone-polygon files produced by the calibration tool"
    group: Zones

  - name: person_class
    label: "Person Class Name"
    type: string
    default: "person"
    description: "Object class name (as emitted by the upstream detection skill) treated as an occupant"
    group: Detection

  - name: min_confidence
    label: "Minimum Confidence"
    type: number
    min: 0.0
    max: 1.0
    default: 0.5
    description: "Ignore upstream detections below this confidence before zone-mapping"
    group: Detection

  - name: anchor_point
    label: "Bbox Anchor Point"
    type: select
    options: ["bottom_center", "center"]
    default: "bottom_center"
    description: "Which point of a person's bounding box is tested against zone polygons (bottom_center approximates foot position)"
    group: Detection

capabilities:
  zone_presence:
    script: scripts/main.py
    description: "Maps upstream person detections into calibrated zone polygons and emits raw zone_covered/zone_gap-shaped presence events (Phase 1: no debounce/threshold — see Phase 2 for the coverage rules engine)"
---

# Floorwatch Coverage

Floorwatch's Part B (coverage) detection path. This skill does **not** run its own model — it is a downstream consumer of a detection skill's output (e.g. `yolo-detection-2026`). It reads `detections` JSON-line events from stdin, maps each `person` bounding box into the calibrated zone polygons for that `camera_id`, debounces per-zone occupancy, and emits schema-validated `zone_covered` / `zone_gap` events (the shared event schema — see `skills/lib/floorwatch_schema.py`) to stdout and, optionally, onto a Redis Stream for the Phase 2 rules engine to consume.

It intentionally contains **no escalation/tiering logic** (nudge timers, supervisor commands, cooldowns) — that lives in the standalone rules engine (`services/floorwatch-rules-engine/`). This skill's only job is turning noisy per-frame detections into stable, schema-compliant zone state transitions. It does not persist video or frames; it only ever reads structured detection JSON already produced upstream and writes structured JSON.

## Pipeline position

```
camera (RTSP) → Aegis → yolo-detection-2026 (frame in, detections out)
                              │
                              ▼  (JSONL: {"event":"detections", ...})
                       floorwatch-coverage (this skill)
                              │
                              ▼  (JSONL: shared-schema zone_covered/zone_gap events)
                     stdout (always) + Redis Stream (if --redis-url set)
                              │
                              ▼
                   floorwatch-rules-engine (Phase 2 — escalation, dashboard WebSocket)
```

## Zone calibration

Zones are drawn with the calibration tool in `calibration/` (a standalone canvas HTML page + tiny local save server — see `calibration/README.md`) and saved as `zones/<camera_id>.json`:

```json
{
  "camera_id": "lobby_cam_1",
  "zones": [
    { "zone_id": "concession_a", "role_tag": "concession", "polygon": [[120,80],[420,80],[420,360],[120,360]] }
  ]
}
```

`polygon` points are pixel coordinates in the same frame space as the upstream detection skill's `bbox` (i.e. the still frame used for calibration must be the same resolution/crop as the live camera feed).

## Protocol

### Upstream skill → this skill (stdin)
Same `detections` event shape as `yolo-detection-2026` (see [detection-protocol.md](../../../docs/detection-protocol.md)):
```jsonl
{"event": "detections", "frame_id": 42, "camera_id": "lobby_cam_1", "timestamp": "2026-07-24T10:00:00Z", "objects": [
  {"class": "person", "confidence": 0.92, "bbox": [100, 50, 300, 400]}
]}
```

### This skill → downstream (stdout, and Redis Stream if configured)
One event **per zone state transition** (not per frame — debounced):
```jsonl
{"event": "ready", "skill": "floorwatch-coverage", "cameras_calibrated": ["lobby_cam_1"], "zone_count": 3, "debounce_seconds": 60, "gap_confidence_threshold": 0.8, "shadow_mode": true, "redis_enabled": true}
{"event_id": "...", "timestamp": "2026-07-24T10:00:00Z", "camera_id": "lobby_cam_1", "zone_id": "concession_a", "role_tag": "concession", "entity_ref": "track_0", "event_type": "zone_covered", "task_id": null, "active_minutes": null, "assigned_minutes": null, "confidence": 0.92, "source_model_version": "floorwatch-coverage/0.2.0"}
{"event": "error", "message": "...", "retriable": true}
```

Every emitted event is validated against the shared schema (`skills/lib/floorwatch_schema.py`, Pydantic) before being written — malformed events are rejected and logged to stderr, never passed downstream.

`entity_ref` is a per-frame anonymous placeholder (`track_<n>` derived from detection order within that frame) — Phase 3+ wires in real cross-frame track IDs from DeepCamera's re-ID, per the global constraint that only anonymous `entity_ref` values are used unless a supervisor has escalated. `zone_gap` events always carry `entity_ref: null` (no one there).

### Debounce behavior
- `zone_covered` fires **immediately** on any frame where a person is detected in the zone with confidence ≥ `gap_confidence_threshold` (default 0.8), and the zone wasn't already covered.
- `zone_gap` only fires after the zone has been continuously unoccupied (or only seeing sub-threshold detections) for ≥ `debounce_seconds` (default 60s) — filtering momentary detection misses/occlusion, per the brief's Phase 2 spec.
- Only one event is emitted per state change, not repeated every frame — the rules engine tracks its own timers off that single event.

### Stop command
```jsonl
{"command": "stop"}
```

## Local testing

```bash
echo '{"event":"detections","camera_id":"lobby_cam_1","timestamp":"2026-07-24T10:00:00Z","objects":[{"class":"person","confidence":0.92,"bbox":[150,200,250,350]}]}' | python scripts/main.py --zones-dir zones
```

See `tests/test_main.py` for zone-mapping, debounce, and event-schema unit tests, and `tests/run_pipeline_test.py` for a full camera→detection→coverage smoke test against a static test image (no real camera feed available for this pilot — see `PHASE_1_NOTES.md`).

## Installation

```bash
./deploy.sh
```

Pure-Python — no GPU/ML dependencies. Requires `pydantic` (schema validation) and `redis` (optional event bus publish), installed via `requirements.txt`.
