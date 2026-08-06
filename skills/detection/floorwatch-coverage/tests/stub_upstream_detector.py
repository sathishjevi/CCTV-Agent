#!/usr/bin/env python3
"""
Stub upstream detector — implements the SAME JSONL protocol as
skills/detection/yolo-detection-2026/scripts/detect.py (ready / frame /
detections / stop), but with deterministic, clearly-labeled synthetic
detections instead of running a real model.

Why this exists: this dev sandbox has no internet access to `pip install
ultralytics torch`, so the real yolo-detection-2026 skill cannot run here.
Per the brief's Global Constraint 4 (flag rather than silently fake
success) and Phase 1's explicit allowance to flag missing real camera
access, `tests/run_pipeline_test.py` uses this stub in place of a real
model UNLESS `ultralytics` is importable, in which case it drives the
real skill instead. Both paths speak the identical protocol, so
floorwatch-coverage/scripts/main.py is exercised unmodified either way.

Detection pattern (by frame_id, 1-indexed), simulating a worker walking
into and out of a zone over a short "shift":
  frames 1-3: person inside the demo zone   (bbox centered ~ (250,300))
  frames 4-6: person outside the demo zone  (bbox centered ~ (700,450))
  frames 7-9: person back inside the demo zone
"""

import sys
import json

MODEL_NAME = "stub-detector-synthetic"


def emit(event: dict):
    print(json.dumps(event), flush=True)


def log(msg: str):
    print(f"[STUB-DETECTOR] {msg}", file=sys.stderr, flush=True)


def bbox_for_frame(frame_id: int):
    cycle = ((frame_id - 1) // 3) % 2
    if cycle == 0:
        return [200, 250, 300, 400]  # inside demo zone
    return [650, 400, 750, 550]      # outside demo zone


def main():
    emit({
        "event": "ready",
        "model": MODEL_NAME,
        "device": "cpu",
        "classes": 1,
        "note": "SYNTHETIC detector for pipeline testing — not a real model",
    })
    log("Synthetic detector ready. This is NOT real object detection — "
        "see docstring in stub_upstream_detector.py")

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

        if msg.get("event") == "frame":
            frame_id = msg.get("frame_id", 1)
            emit({
                "event": "detections",
                "frame_id": frame_id,
                "camera_id": msg.get("camera_id", "unknown"),
                "timestamp": msg.get("timestamp", ""),
                "objects": [
                    {"class": "person", "confidence": 0.91, "bbox": bbox_for_frame(frame_id)},
                ],
            })


if __name__ == "__main__":
    main()
