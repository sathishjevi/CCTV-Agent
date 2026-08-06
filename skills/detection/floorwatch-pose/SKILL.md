---
name: floorwatch-pose
description: "Floorwatch Pose — per-frame motion/activity signal distinguishing active movement from standing-still, for Part A effort tracking"
version: 0.1.0
icon: assets/icon.png
entry: scripts/pose.py
deploy: deploy.sh

requirements:
  python: ">=3.9"
  mediapipe: ">=0.10"
  platforms: ["linux", "macos", "windows"]

parameters:
  - name: auto_start
    label: "Auto Start"
    type: boolean
    default: false
    description: "Start this skill automatically when Aegis launches"
    group: Lifecycle

  - name: model_path
    label: "Pose Model Path"
    type: string
    default: "models/pose_landmarker_lite.task"
    description: "Path to the MediaPipe PoseLandmarker .task bundle (downloaded by deploy.sh). If missing, falls back to a frame-differencing motion proxy — see 'Fallback mode' below."
    group: Model

  - name: active_threshold
    label: "Active Motion Threshold"
    type: number
    min: 0.0
    max: 1.0
    default: 0.15
    description: "Normalized motion score above which a frame counts as 'active' rather than idle"
    group: Model

  - name: fps
    label: "Processing FPS"
    type: select
    options: [0.2, 0.5, 1, 3, 5]
    default: 1
    description: "Pose/motion is far cheaper to sample sparsely than object detection — 1 fps is usually enough to classify active vs idle over a task window"
    group: Performance

capabilities:
  motion_signal:
    script: scripts/pose.py
    description: "Per-frame motion/activity classification for effort (Part A) tracking"
---

# Floorwatch Pose

Part A's activity signal. DeepCamera's YOLO detection skill gives *presence*
(is a person in frame/zone) but not *activity* (are they moving or standing
still) — this skill reads the same camera frames DeepCamera hands to
detection skills and produces a per-frame motion score, per the build
brief's Phase 3 task 2 ("integrate a separate lightweight pose model...
to distinguish active movement from standing-still/idle").

It does not do zone mapping or task accounting itself — that's the rules
engine's `EffortEngine` (`services/floorwatch-rules-engine/app/effort_engine.py`),
which correlates this skill's motion signal with `floorwatch-coverage`'s
zone-occupancy state and each open task's assignment window. No video/frames
are persisted by this skill — only the derived numeric score.

## Two execution modes

1. **Real (MediaPipe PoseLandmarker, Tasks API)** — used when
   `models/pose_landmarker_lite.task` is present. Tracks landmark positions
   frame-to-frame per camera and scores motion as normalized mean landmark
   displacement.
2. **Fallback (frame-differencing motion proxy)** — used automatically when
   the model file is absent or MediaPipe fails to load. Computes a simple
   normalized mean-absolute-pixel-difference between consecutive frames per
   camera using PIL + numpy (no ML model). This is a real, working motion
   signal — just cruder than pose landmark tracking (it can't distinguish
   "person moving" from "someone else walked through the background"), and
   is flagged as `"mode": "fallback"` in every `ready`/`pose_motion` event
   so downstream consumers and dashboards can tell which mode produced the
   data.

**Why the fallback exists in this pilot's dev environment**: downloading
the `.task` model bundle requires internet access to
`storage.googleapis.com`, which this sandbox's network policy blocks (SSL
interception on arbitrary hosts). `deploy.sh` attempts the download and
prints a clear warning + falls back automatically if it fails — this is a
real environment limitation, not a shortcut taken silently. See
`PHASE_3_NOTES.md` at the repo root.

## Protocol

Same `frame`-event JSONL protocol as `yolo-detection-2026`
(see `docs/detection-protocol.md`) — Aegis fans the same camera frame out
to both skills.

### Aegis → skill (stdin)
```jsonl
{"event": "frame", "frame_id": 42, "camera_id": "lobby_cam_1", "timestamp": "...", "frame_path": "/tmp/aegis_detection/frame_lobby_cam_1.jpg", "width": 1920, "height": 1080}
```

### Skill → downstream (stdout, and Redis Stream `floorwatch:motion` if `--redis-url` set)
```jsonl
{"event": "ready", "skill": "floorwatch-pose", "mode": "real", "model": "pose_landmarker_lite", "fps": 1}
{"event": "pose_motion", "frame_id": 42, "camera_id": "lobby_cam_1", "timestamp": "...", "motion_score": 0.31, "active": true, "mode": "real"}
{"event": "error", "frame_id": 42, "message": "...", "retriable": true}
```

`motion_score` is a per-camera signal (0.0–1.0, higher = more movement
since the previous sampled frame) — not per-person. This is a Phase 3
approximation: it doesn't isolate which specific person in a multi-person
frame is moving. Combined with `floorwatch-coverage`'s zone occupancy
downstream, the effort engine treats "zone occupied AND motion_score above
threshold" as active time for whatever task is open in that zone — good
enough for a single-occupant-zone pilot, not a substitute for real
per-person pose tracking at scale.

### Stop command
```jsonl
{"command": "stop"}
```

## Local testing

```bash
echo '{"event":"frame","frame_id":1,"camera_id":"lobby_cam_1","timestamp":"2026-07-24T10:00:00Z","frame_path":"/path/to/frame.jpg","width":960,"height":540}' | python scripts/pose.py
```

## Installation

```bash
./deploy.sh
```

Attempts to download the MediaPipe pose model; continues in fallback mode
with a warning if that fails (e.g. restricted network).
