"""ingest.py — Floorwatch Ingest entrypoint.

Reads a cameras.json manifest, instantiates the right FrameSource per camera
(RTSP / local folder / S3 / Azure Blob / GCS / third-party HTTP API), and
emits the standard `frame` JSONL protocol (docs/detection-protocol.md) to
stdout — identical to what Aegis itself would send to a detection skill's
stdin. Downstream (detect.py, floorwatch-coverage, floorwatch-pose) never
needs to know which source type produced a given frame.

Each camera's FrameSource.frames() is a long-running generator (its own
polling/backoff loop), so cameras run concurrently in one thread each; a
shared queue funnels their Frame objects onto a single stdout stream,
serialized with an incrementing frame_id.

Usage:
    python ingest.py --cameras cameras.json
    python ingest.py --cameras cameras.json | python ../../yolo-detection-2026/scripts/detect.py
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))

from sources.base import Frame
from sources.local_folder import LocalFolderFrameSource
from sources.rtsp import RtspFrameSource
from sources.cloud_storage import build_cloud_storage_source
from sources.http_api import HttpApiFrameSource
from sources.ezviz import EzvizFrameSource

try:
    from floorwatch_secrets_guard import load_deployment_config
except ImportError:
    load_deployment_config = None

LOG_PREFIX = "[floorwatch-ingest]"

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def log(msg):
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


def _expand_env_value(value, camera_id):
    """Replaces ${VAR_NAME} in a string with os.environ[VAR_NAME]. This is
    how secrets (connection strings, API tokens) get into cameras.json
    without ever being written into the file itself — set the real value in
    config/secrets.env instead (see config/README.md) and reference it here
    by name."""
    def _sub(match):
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            log(f"WARNING: camera '{camera_id}' references ${{{var_name}}} but it is not set "
                f"in the environment or config/secrets.env — leaving blank")
            return ""
        return val
    return _ENV_VAR_PATTERN.sub(_sub, value)


def expand_env(obj, camera_id):
    if isinstance(obj, str):
        return _expand_env_value(obj, camera_id)
    if isinstance(obj, dict):
        return {k: expand_env(v, camera_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, camera_id) for v in obj]
    return obj


def build_source(cam):
    camera_id = cam["camera_id"]
    source_type = cam["source_type"]
    fps = cam.get("fps", 1.0)

    if source_type == "rtsp":
        cfg = cam.get("rtsp", {})
        return RtspFrameSource(
            camera_id=camera_id,
            url=cfg["url"],
            fps=fps,
            max_backoff_seconds=cfg.get("max_backoff_seconds", 30.0),
        )

    if source_type == "local_folder":
        cfg = cam.get("local_folder", {})
        return LocalFolderFrameSource(
            camera_id=camera_id,
            folder=cfg["folder"],
            fps=fps,
            poll_interval_seconds=cfg.get("poll_interval_seconds", 5.0),
            state_file=cfg.get("state_file"),
        )

    if source_type in ("s3", "azure_blob", "gcs"):
        cfg = cam.get(source_type, {})
        return build_cloud_storage_source(
            provider=source_type,
            camera_id=camera_id,
            fps=fps,
            **cfg,
        )

    if source_type == "http_api":
        cfg = cam.get("http_api", {})
        return HttpApiFrameSource(
            camera_id=camera_id,
            fps=fps,
            **cfg,
        )

    if source_type == "ezviz":
        cfg = cam.get("ezviz", {})
        return EzvizFrameSource(
            camera_id=camera_id,
            fps=fps,
            **cfg,
        )

    raise ValueError(
        f"Unknown source_type '{source_type}' for camera '{camera_id}' "
        f"(expected one of: rtsp, local_folder, s3, azure_blob, gcs, http_api, ezviz)"
    )


def run_camera(source, camera_id, out_queue, stop_event):
    try:
        for frame in source.frames():
            if stop_event.is_set():
                break
            out_queue.put(("frame", frame))
    except Exception as exc:
        out_queue.put(("error", (camera_id, str(exc))))
    finally:
        try:
            source.close()
        except Exception:
            pass


def watch_stdin_for_stop(stop_event):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("command") == "stop":
            stop_event.set()
            return


def main():
    parser = argparse.ArgumentParser(description="Floorwatch multi-source CCTV ingest")
    parser.add_argument("--cameras", default="cameras.json", help="Path to cameras.json manifest")
    args = parser.parse_args()

    manifest_path = Path(args.cameras)
    if not manifest_path.exists():
        print(json.dumps({
            "event": "error",
            "message": f"Cameras manifest not found: {manifest_path}",
            "retriable": False,
        }), flush=True)
        sys.exit(1)

    if load_deployment_config:
        load_deployment_config(REPO_ROOT)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cameras = manifest.get("cameras", [])
    if not cameras:
        print(json.dumps({
            "event": "error",
            "message": "cameras.json has no cameras configured",
            "retriable": False,
        }), flush=True)
        sys.exit(1)

    out_queue = queue.Queue()
    stop_event = threading.Event()
    threads = []
    camera_ids = []

    for cam in cameras:
        camera_id = cam["camera_id"]
        cam = expand_env(cam, camera_id)
        try:
            source = build_source(cam)
        except Exception as exc:
            log(f"Failed to build source for camera '{camera_id}': {exc}")
            print(json.dumps({
                "event": "error",
                "message": f"camera '{camera_id}': {exc}",
                "retriable": False,
            }), flush=True)
            continue
        camera_ids.append(camera_id)
        t = threading.Thread(
            target=run_camera,
            args=(source, camera_id, out_queue, stop_event),
            daemon=True,
            name=f"ingest-{camera_id}",
        )
        threads.append(t)

    if not threads:
        print(json.dumps({
            "event": "error",
            "message": "No cameras started successfully",
            "retriable": False,
        }), flush=True)
        sys.exit(1)

    print(json.dumps({
        "event": "ready",
        "cameras": camera_ids,
        "count": len(camera_ids),
    }), flush=True)
    log(f"Started {len(threads)} camera source(s): {', '.join(camera_ids)}")

    for t in threads:
        t.start()

    stdin_watcher = threading.Thread(target=watch_stdin_for_stop, args=(stop_event,), daemon=True)
    stdin_watcher.start()

    frame_id = 0
    try:
        while not stop_event.is_set():
            try:
                kind, payload = out_queue.get(timeout=0.5)
            except queue.Empty:
                if not any(t.is_alive() for t in threads):
                    log("All camera sources have stopped; exiting.")
                    break
                continue

            if kind == "frame":
                frame: Frame = payload
                frame_id += 1
                event = {
                    "event": "frame",
                    "frame_id": frame_id,
                    "camera_id": frame.camera_id,
                    "timestamp": frame.timestamp,
                    "frame_path": frame.frame_path,
                }
                if frame.width is not None:
                    event["width"] = frame.width
                if frame.height is not None:
                    event["height"] = frame.height
                print(json.dumps(event), flush=True)
            elif kind == "error":
                camera_id, message = payload
                log(f"Camera '{camera_id}' stopped with error: {message}")
                print(json.dumps({
                    "event": "error",
                    "message": f"camera '{camera_id}': {message}",
                    "retriable": False,
                }), flush=True)
    except KeyboardInterrupt:
        log("Interrupted, shutting down...")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
        log("Stopped.")


if __name__ == "__main__":
    main()
