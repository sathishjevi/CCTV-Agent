#!/usr/bin/env python3
"""
Phase 1 end-to-end pipeline smoke test.

Proves the data path required by Phase 1's acceptance criteria:

    camera (test feed) -> DeepCamera detection skill -> Floorwatch skill -> console log

producing correctly zone-mapped presence events.

IMPORTANT — explicit flag per the brief's Global Constraint 4 (never
silently fake success):
  * No real cineplex cameras/RTSP feeds are available in this environment.
    A single still test frame (docker/od/panda.jpg) stands in for a camera
    capture.
  * This dev sandbox has no internet access, so `ultralytics`/`torch`
    cannot be installed here. If they ARE importable, this harness drives
    the REAL skills/detection/yolo-detection-2026/scripts/detect.py.
    Otherwise it falls back to tests/stub_upstream_detector.py, a
    deterministic synthetic-detection double that speaks the exact same
    JSONL protocol — floorwatch-coverage/scripts/main.py itself is
    exercised unmodified either way, only the upstream is swapped.
  * A demo zone calibration file is auto-written to tests/demo_zones/
    (not the real skill's zones/ dir) so this test never touches real
    calibration data.

Usage:
  python run_pipeline_test.py
  python run_pipeline_test.py --force-real-yolo   # error out instead of falling back
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import importlib.util
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
DEMO_ZONES_DIR = TESTS_DIR / "demo_zones"
DEMO_FRAME_DIR = Path.home() / ".floorwatch_test" if False else Path(TESTS_DIR / "_tmp_frames")
CAMERA_ID = "demo_cam"

REAL_DETECT_PY = REPO_ROOT / "skills" / "detection" / "yolo-detection-2026" / "scripts" / "detect.py"
STUB_DETECT_PY = TESTS_DIR / "stub_upstream_detector.py"
MAIN_PY = SKILL_DIR / "scripts" / "main.py"

DEMO_ZONE_FILE_CONTENT = {
    "camera_id": CAMERA_ID,
    "zones": [
        {
            "zone_id": "concession_a",
            "role_tag": "concession",
            # matches tests/stub_upstream_detector.py's "inside" bbox anchor (~250,400)
            # and excludes its "outside" bbox anchor (~700,550)
            "polygon": [[100, 150], [400, 150], [400, 500], [100, 500]],
        }
    ],
}


def ultralytics_available() -> bool:
    return importlib.util.find_spec("ultralytics") is not None


def provision_demo_zone():
    DEMO_ZONES_DIR.mkdir(parents=True, exist_ok=True)
    zone_file = DEMO_ZONES_DIR / f"{CAMERA_ID}.json"
    zone_file.write_text(json.dumps(DEMO_ZONE_FILE_CONTENT, indent=2))
    return zone_file


def provision_test_frame() -> Path:
    """Stand-in for a live camera capture — see module docstring."""
    DEMO_FRAME_DIR.mkdir(parents=True, exist_ok=True)
    src = REPO_ROOT / "docker" / "od" / "panda.jpg"
    dst = DEMO_FRAME_DIR / f"frame_{CAMERA_ID}.jpg"
    if src.exists():
        shutil.copyfile(src, dst)
    else:
        # No sample image found either — write a tiny valid JPEG so the
        # pipeline still runs; detections will just be whatever the
        # detector (real or stub) produces for an arbitrary image.
        import struct
        dst.write_bytes(bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300"
            + "10" * 63 + "ffd9"
        ))
    return dst


def main():
    parser = argparse.ArgumentParser(description="Floorwatch Phase 1 pipeline smoke test")
    parser.add_argument("--frame-count", type=int, default=9)
    parser.add_argument("--force-real-yolo", action="store_true",
                        help="Fail instead of falling back to the synthetic stub detector")
    args = parser.parse_args()

    use_real = ultralytics_available()
    if args.force_real_yolo and not use_real:
        print("FATAL: --force-real-yolo set but `ultralytics` is not importable "
              "(no internet access to pip install in this environment).", file=sys.stderr)
        sys.exit(1)

    upstream_cmd = (
        [sys.executable, str(REAL_DETECT_PY), "--model-size", "nano", "--confidence", "0.5"]
        if use_real else
        [sys.executable, str(STUB_DETECT_PY)]
    )
    print(f"=== Floorwatch Phase 1 pipeline smoke test ===")
    print(f"Upstream detector : {'REAL yolo-detection-2026' if use_real else 'SYNTHETIC stub (see stub_upstream_detector.py docstring — no ultralytics/torch available)'}")
    print(f"Camera feed        : test still image (no real RTSP camera available in this environment)")
    zone_file = provision_demo_zone()
    print(f"Zone calibration   : demo zones auto-written to {zone_file} (not the real skill zones/ dir)")
    frame_path = provision_test_frame()
    print(f"Test frame         : {frame_path}")
    print()

    upstream = subprocess.Popen(
        upstream_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    coverage = subprocess.Popen(
        [sys.executable, str(MAIN_PY), "--zones-dir", str(DEMO_ZONES_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )

    # consume 'ready' events from both stages
    upstream_ready = json.loads(upstream.stdout.readline())
    coverage_ready = json.loads(coverage.stdout.readline())
    print(f"[upstream ready]  {upstream_ready}")
    print(f"[coverage ready]  {coverage_ready}")
    print()

    zone_events = []
    for frame_id in range(1, args.frame_count + 1):
        frame_msg = {
            "event": "frame",
            "frame_id": frame_id,
            "camera_id": CAMERA_ID,
            "timestamp": f"2026-07-24T10:00:{frame_id:02d}Z",
            "frame_path": str(frame_path),
            "width": 960,
            "height": 540,
        }
        upstream.stdin.write(json.dumps(frame_msg) + "\n")
        upstream.stdin.flush()

        det_line = upstream.stdout.readline()
        detections = json.loads(det_line)
        if detections.get("event") != "detections":
            print(f"  frame {frame_id}: unexpected upstream event: {detections}")
            continue

        coverage.stdin.write(json.dumps(detections) + "\n")
        coverage.stdin.flush()

        presence_line = coverage.stdout.readline()
        presence = json.loads(presence_line)
        zone_events.append(presence)
        state = "OCCUPIED" if presence.get("occupied") else "gap"
        print(f"  frame {frame_id}: zone={presence['zone_id']:<14} {state:<9} "
              f"entity_refs={presence['entity_refs']}")

    upstream.stdin.write(json.dumps({"command": "stop"}) + "\n")
    upstream.stdin.flush()
    coverage.stdin.write(json.dumps({"command": "stop"}) + "\n")
    coverage.stdin.flush()
    time.sleep(0.2)
    upstream.terminate()
    coverage.terminate()

    print()
    occupied_count = sum(1 for e in zone_events if e.get("occupied"))
    gap_count = len(zone_events) - occupied_count
    print(f"=== Result: {len(zone_events)} zone_presence events emitted "
          f"({occupied_count} occupied, {gap_count} gap) ===")

    # Sanity assertion — the demo detection pattern must produce both
    # occupied and gap events, proving zone-mapping actually discriminates.
    if occupied_count == 0 or gap_count == 0:
        print("FAIL: expected both occupied and gap events from the demo pattern", file=sys.stderr)
        sys.exit(1)
    print("PASS: pipeline produced correctly zone-mapped presence events for both states.")


if __name__ == "__main__":
    main()
