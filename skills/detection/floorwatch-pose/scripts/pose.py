#!/usr/bin/env python3
"""
Floorwatch Pose Skill — per-frame motion/activity signal for SharpAI Aegis.

Communicates via JSON lines over stdin/stdout, same `frame`-event protocol
as yolo-detection-2026 (docs/detection-protocol.md) — Aegis fans the same
camera frame out to both skills.

Two modes (see SKILL.md "Two execution modes" for full rationale):
  - "real": MediaPipe PoseLandmarker (Tasks API) — used when the .task
    model bundle is present at --model-path.
  - "fallback": PIL+numpy frame-differencing motion proxy — used
    automatically when the model is missing or fails to load. A real,
    working (if cruder) motion signal, not a fake one. Every emitted event
    is tagged with "mode" so downstream consumers know which produced it.

Usage:
  python pose.py --model-path models/pose_landmarker_lite.task
  echo '{"event":"frame",...}' | python pose.py
"""

import sys
import os
import json
import argparse
import signal
from pathlib import Path

SKILL_VERSION = "0.1.0"


def emit(event: dict):
    print(json.dumps(event), flush=True)


def log(msg: str):
    print(f"[FLOORWATCH-POSE] {msg}", file=sys.stderr, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Floorwatch Pose Skill")
    parser.add_argument("--config", type=str, help="Path to config JSON file")
    parser.add_argument("--model-path", type=str, default="models/pose_landmarker_lite.task")
    parser.add_argument("--active-threshold", type=float, default=0.15)
    parser.add_argument("--fps", type=float, default=1)
    parser.add_argument("--redis-url", type=str, default=None)
    parser.add_argument("--redis-stream", type=str, default="floorwatch:motion")
    return parser.parse_args()


def load_config(args):
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
        "model_path": args.model_path,
        "active_threshold": args.active_threshold,
        "fps": args.fps,
        "redis_url": args.redis_url,
        "redis_stream": args.redis_stream,
    }


def make_redis_publisher(redis_url: str, stream: str):
    import redis
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.ping()

    def publish(event: dict):
        client.xadd(stream, {"data": json.dumps(event)})

    return publish


# ── Real mode: MediaPipe PoseLandmarker ──────────────────────────────────

class RealPoseEstimator:
    """MediaPipe PoseLandmarker (Tasks API), IMAGE running mode — one call
    per frame file. Motion score = normalized mean landmark displacement
    vs. that camera's previous sampled frame."""

    def __init__(self, model_path: str):
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions
        import mediapipe as mp

        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=1,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)
        self._mp_image_cls = mp.Image
        self._image_format = mp.ImageFormat.SRGB
        self._prev_landmarks_by_camera = {}

    def score(self, camera_id: str, frame_path: str):
        from PIL import Image
        import numpy as np

        img = Image.open(frame_path).convert("RGB")
        mp_image = self._mp_image_cls(image_format=self._image_format, data=np.asarray(img))
        result = self._landmarker.detect(mp_image)

        if not result.pose_landmarks:
            return 0.0, False  # no person detected this frame — no motion to score

        landmarks = result.pose_landmarks[0]
        coords = np.array([[lm.x, lm.y] for lm in landmarks])

        prev = self._prev_landmarks_by_camera.get(camera_id)
        self._prev_landmarks_by_camera[camera_id] = coords
        if prev is None or prev.shape != coords.shape:
            return 0.0, False  # first sighting for this camera — nothing to compare yet

        mean_displacement = float(np.mean(np.linalg.norm(coords - prev, axis=1)))
        # Heuristic normalization: normalized landmark coords are in [0,1];
        # real inter-frame movement for an active person rarely exceeds
        # ~0.2 mean displacement at 1fps sampling, so scale by 5x and clip.
        motion_score = min(1.0, mean_displacement * 5.0)
        return motion_score, True


# ── Fallback mode: frame differencing ────────────────────────────────────

class FallbackMotionEstimator:
    """No ML model — mean absolute grayscale pixel difference between
    consecutive sampled frames per camera, downscaled for speed. A real
    motion signal, just one that can't distinguish a person moving from
    any other change in the frame (lighting, background motion)."""

    THUMB_SIZE = (64, 64)

    def __init__(self):
        self._prev_by_camera = {}

    def score(self, camera_id: str, frame_path: str):
        from PIL import Image
        import numpy as np

        img = Image.open(frame_path).convert("L").resize(self.THUMB_SIZE)
        arr = np.asarray(img, dtype=np.float32)

        prev = self._prev_by_camera.get(camera_id)
        self._prev_by_camera[camera_id] = arr
        if prev is None:
            return 0.0, False

        mean_abs_diff = float(np.mean(np.abs(arr - prev)))
        # Heuristic normalization: typical frame-to-frame noise is a few
        # units out of 255; real motion often pushes mean diff into the
        # 10-40 range. Scale so ~30 maps near 1.0.
        motion_score = min(1.0, mean_abs_diff / 30.0)
        return motion_score, True


def build_estimator(model_path: str):
    model_file = Path(model_path)
    if not model_file.exists():
        log(f"Model file not found at {model_path} — using fallback frame-differencing mode. "
            f"See SKILL.md 'Two execution modes' for how to enable real pose tracking.")
        return FallbackMotionEstimator(), "fallback"

    try:
        return RealPoseEstimator(str(model_file)), "real"
    except Exception as e:
        log(f"Failed to load MediaPipe PoseLandmarker ({e}) — using fallback frame-differencing mode.")
        return FallbackMotionEstimator(), "fallback"


def main():
    args = parse_args()
    config = load_config(args)

    model_path = config.get("model_path", "models/pose_landmarker_lite.task")
    active_threshold = config.get("active_threshold", 0.15)
    fps = config.get("fps", 1)
    redis_url = config.get("redis_url")
    redis_stream = config.get("redis_stream", "floorwatch:motion")

    estimator, mode = build_estimator(model_path)

    publish = None
    if redis_url:
        try:
            publish = make_redis_publisher(redis_url, redis_stream)
            log(f"Publishing events to Redis stream '{redis_stream}' at {redis_url}")
        except Exception as e:
            log(f"WARNING: could not connect to Redis at {redis_url}: {e} — continuing stdout-only")

    emit({
        "event": "ready",
        "skill": "floorwatch-pose",
        "version": SKILL_VERSION,
        "mode": mode,
        "fps": fps,
        "active_threshold": active_threshold,
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

        if msg.get("event") == "frame":
            frame_path = msg.get("frame_path")
            frame_id = msg.get("frame_id")
            camera_id = msg.get("camera_id", "unknown")
            timestamp = msg.get("timestamp", "")

            if not frame_path or not Path(frame_path).exists():
                emit({"event": "error", "frame_id": frame_id,
                      "message": f"Frame not found: {frame_path}", "retriable": True})
                continue

            try:
                motion_score, has_baseline = estimator.score(camera_id, frame_path)
            except Exception as e:
                emit({"event": "error", "frame_id": frame_id,
                      "message": f"Motion scoring error: {e}", "retriable": True})
                continue

            evt = {
                "event": "pose_motion",
                "frame_id": frame_id,
                "camera_id": camera_id,
                "timestamp": timestamp,
                "motion_score": round(motion_score, 4),
                "active": has_baseline and motion_score >= active_threshold,
                "mode": mode,
            }
            emit(evt)
            if publish:
                publish(evt)


if __name__ == "__main__":
    main()
