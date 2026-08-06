"""run_pipeline.py — all-in-one local Floorwatch pipeline orchestrator.

Wires ingestion -> detection -> coverage + pose -> Redis as ONE command,
for a client (or a local test) that doesn't want to hand-assemble shell
pipes. Each stage is the same standalone script Aegis itself would run —
this script just Popen's all four and forwards JSONL between them:

    ingest.py --cameras cameras.json
        -> (fan out "frame" events to both of the below)
        -> detect.py                       -> (forward "detections" events)
            -> floorwatch-coverage/main.py  -> Redis stream floorwatch:events
        -> floorwatch-pose/pose.py          -> Redis stream floorwatch:motion

floorwatch-coverage and floorwatch-pose push to Redis themselves (via
their own --redis-url/--redis-stream flags) — this script does not touch
Redis directly, it only plumbs stdin/stdout between processes.

`ingest.py` standalone (piped by hand into detect.py) remains the right
choice when Aegis itself is doing real-camera ingestion; this orchestrator
is for a client/test setup that wants one command instead of a shell
pipeline, or that needs the frame fan-out to both detect.py and pose.py
that a plain shell pipe can't express.

Usage:
    python tools/run_pipeline.py --cameras skills/detection/floorwatch-ingest/cameras.json

All stages run under the same Python interpreter that launches this
script (sys.executable) — install every stage's requirements.txt into
one environment before running. See CCTV_INTEGRATION_SETUP.md.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))

from floorwatch_secrets_guard import load_deployment_config  # noqa: E402

INGEST_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-ingest" / "scripts" / "ingest.py"
DETECT_SCRIPT = REPO_ROOT / "skills" / "detection" / "yolo-detection-2026" / "scripts" / "detect.py"
COVERAGE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-coverage" / "scripts" / "main.py"
POSE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-pose" / "scripts" / "pose.py"


def log(tag, msg):
    print(f"[run_pipeline:{tag}] {msg}", file=sys.stderr, flush=True)


def spawn(tag, cmd, stdin_pipe):
    log(tag, f"starting: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_pipe else None,
        stdout=subprocess.PIPE,
        stderr=None,  # inherit — each stage's own [tag] stderr logging passes through directly
        text=True,
        bufsize=1,
    )


def pump(tag, proc, forward_event, targets, stop_event):
    """Read JSONL lines from proc.stdout; lines whose "event" is in
    forward_event get written to every stream in targets, everything else
    is just logged."""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or stop_event.is_set():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(tag, f"non-JSON line: {line[:200]}")
                continue

            event = msg.get("event")
            if event in forward_event:
                for target in targets:
                    try:
                        target.write(line + "\n")
                        target.flush()
                    except (BrokenPipeError, ValueError):
                        pass
            elif event == "error":
                log(tag, f"ERROR: {msg.get('message')}")
            elif event in ("ready", "progress", "complete"):
                log(tag, f"{event}: {msg}")
            # perf_stats / other informational events: silently dropped from stderr noise
    except ValueError:
        pass  # stdout closed during shutdown


def build_argv(python, script, extra_args):
    return [python, str(script)] + extra_args


def main():
    parser = argparse.ArgumentParser(description="Floorwatch all-in-one local pipeline orchestrator")
    parser.add_argument("--cameras", required=True, help="Path to cameras.json manifest (see cameras.json.template)")
    parser.add_argument("--redis-url", default=None, help="Overrides FLOORWATCH_REDIS_URL from config/deployment.env")
    parser.add_argument("--events-stream", default=None, help="Overrides FLOORWATCH_REDIS_STREAM")
    parser.add_argument("--motion-stream", default=None, help="Overrides FLOORWATCH_REDIS_MOTION_STREAM")
    parser.add_argument("--zones-dir", default="zones",
                         help="Path to calibrated zone polygons directory — relative paths resolve against "
                              "floorwatch-coverage's own scripts/ dir (its own convention), not this script's cwd; "
                              "pass an absolute path to point elsewhere")
    parser.add_argument("--model-size", default="nano", choices=["nano", "small", "medium", "large"])
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--detect-fps", type=float, default=5)
    parser.add_argument("--pose-fps", type=float, default=1)
    parser.add_argument("--skip-pose", action="store_true", help="Run detection+coverage only, no pose/motion stage")
    parser.add_argument("--shadow-mode", action="store_true", default=True,
                         help="Coverage stage runs in shadow mode (default; matches Global Constraint 4)")
    args = parser.parse_args()

    load_deployment_config(REPO_ROOT)
    redis_url = args.redis_url or os.environ.get("FLOORWATCH_REDIS_URL", "redis://localhost:6379/0")
    events_stream = args.events_stream or os.environ.get("FLOORWATCH_REDIS_STREAM", "floorwatch:events")
    motion_stream = args.motion_stream or os.environ.get("FLOORWATCH_REDIS_MOTION_STREAM", "floorwatch:motion")

    cameras_path = Path(args.cameras)
    if not cameras_path.exists():
        log("main", f"FATAL: cameras manifest not found: {cameras_path}")
        sys.exit(1)

    python = sys.executable
    stop_event = threading.Event()

    ingest_proc = spawn("ingest", build_argv(python, INGEST_SCRIPT, ["--cameras", str(cameras_path)]), stdin_pipe=True)
    detect_proc = spawn("detect", build_argv(python, DETECT_SCRIPT, [
        "--model-size", args.model_size,
        "--confidence", str(args.confidence),
        "--device", args.device,
        "--fps", str(args.detect_fps),
    ]), stdin_pipe=True)
    coverage_argv = [
        "--zones-dir", args.zones_dir,
        "--redis-url", redis_url,
        "--redis-stream", events_stream,
    ]
    if args.shadow_mode:
        coverage_argv.append("--shadow-mode")
    coverage_proc = spawn("coverage", build_argv(python, COVERAGE_SCRIPT, coverage_argv), stdin_pipe=True)

    pose_proc = None
    if not args.skip_pose:
        pose_proc = spawn("pose", build_argv(python, POSE_SCRIPT, [
            "--fps", str(args.pose_fps),
            "--redis-url", redis_url,
            "--redis-stream", motion_stream,
        ]), stdin_pipe=True)

    procs = [p for p in (ingest_proc, detect_proc, coverage_proc, pose_proc) if p]

    threads = [
        threading.Thread(
            target=pump,
            args=("ingest", ingest_proc, {"frame"},
                  [detect_proc.stdin] + ([pose_proc.stdin] if pose_proc else []),
                  stop_event),
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=("detect", detect_proc, {"detections"}, [coverage_proc.stdin], stop_event),
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=("coverage", coverage_proc, set(), [], stop_event),
            daemon=True,
        ),
    ]
    if pose_proc:
        threads.append(threading.Thread(
            target=pump,
            args=("pose", pose_proc, set(), [], stop_event),
            daemon=True,
        ))

    for t in threads:
        t.start()

    log("main", f"Pipeline running (redis={redis_url}, events_stream={events_stream}"
                 f"{', motion_stream=' + motion_stream if pose_proc else ' [pose disabled]'}). Press Ctrl+C to stop.")

    def shutdown(*_):
        if stop_event.is_set():
            return
        log("main", "Shutting down pipeline...")
        stop_event.set()
        stop_msg = json.dumps({"command": "stop"}) + "\n"
        for proc in procs:
            try:
                proc.stdin.write(stop_msg)
                proc.stdin.flush()
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        ingest_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        log("main", "Stopped.")


if __name__ == "__main__":
    main()
