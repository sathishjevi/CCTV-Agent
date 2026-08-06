"""
Full shadow-mode end-to-end test for Part A (effort tracking), mirroring
test_shadow_mode_e2e.py's approach for Part B: wires together the REAL
pieces, not mocks.

  1. skills/detection/floorwatch-pose/scripts/pose.py — spawned as an
     actual subprocess, fed synthetic `frame` events exactly as Aegis
     would, publishing motion events onto a Redis Stream. Runs in
     fallback mode (frame-differencing) since no MediaPipe model file is
     available in this sandbox — see that skill's SKILL.md.
  2. A fakeredis TcpFakeServer standing in for Redis (same rationale as
     the Part B test and skills/.../test_redis_publish.py — no Docker
     here).
  3. The real EffortEngine consuming that motion stream and running the
     assign -> accumulate -> mid-task-nudge -> complete -> flag sequence
     on real wall-clock timers (shortened for test speed).

Asserts the mid-task nudge is marked shadow_mode_suppressed=True and that
completing a low-activity task produces a task_flag written to the shift
digest — the pipeline observes and logs everything but never actually
sends a notification, per Global Constraint 4.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
POSE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-pose" / "scripts" / "pose.py"
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("PIL")

from digest_store import DigestStore  # noqa: E402
from effort_engine import EffortEngine  # noqa: E402


class AllStaffedRoster:
    def is_zone_staffed(self, zone_id: str) -> bool:
        return True


ZONES_META = {"theatre3": {"name": "Theatre 3 (post-show)", "role_tag": "janitor", "camera_id": "theatre3_cam_1"}}
THRESHOLDS = {"_default": {"expected_active_ratio": 0.5}, "clean_door": {"expected_active_ratio": 0.5}}


def _make_image(path: Path, fill: int):
    from PIL import Image
    Image.new("L", (64, 64), color=fill).save(path)


def test_full_pipeline_shadow_mode_low_effort_to_flag(tmp_path):
    # ── 1. fake Redis ────────────────────────────────────────────────
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    redis_url = f"redis://127.0.0.1:{port}/0"

    # ── 2. real floorwatch-pose subprocess (fallback mode) ────────────
    pose = subprocess.Popen(
        [sys.executable, str(POSE_SCRIPT),
         "--model-path", str(tmp_path / "nonexistent.task"),
         "--active-threshold", "0.1",
         "--redis-url", redis_url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    ready = json.loads(pose.stdout.readline())
    assert ready["mode"] == "fallback"
    assert ready["redis_enabled"] is True

    img1 = tmp_path / "f1.jpg"
    img2 = tmp_path / "f2.jpg"
    _make_image(img1, 0)
    _make_image(img2, 255)  # maximally different -> high motion score

    for i, img in enumerate([img1, img2], start=1):
        pose.stdin.write(json.dumps({
            "event": "frame", "frame_id": i, "camera_id": "theatre3_cam_1",
            "timestamp": "2026-07-24T18:00:00Z", "frame_path": str(img),
        }) + "\n")
        pose.stdin.flush()
        pose.stdout.readline()  # consume the pose_motion event

    pose.stdin.write(json.dumps({"command": "stop"}) + "\n")
    pose.stdin.flush()
    pose.terminate()

    # ── 3. real EffortEngine consuming the real Redis stream ──────────
    digest = DigestStore(tmp_path / "digest.jsonl")
    engine = EffortEngine(
        roster=AllStaffedRoster(),
        digest=digest,
        zones_meta=ZONES_META,
        task_type_thresholds=THRESHOLDS,
        shadow_mode=True,  # the point of this test
        update_interval_seconds=1,
        max_motion_gap_seconds=15,
        nudge_grace_ratio=0.0,   # no grace period, so the short test task nudges immediately
        nudge_margin=0.1,
        zone_is_covered=lambda zone_id: True,
    )

    task_evt = engine.assign_task("Clean Door — Zone 4", "theatre3", assigned_minutes=0.02, task_type="clean_door")
    assert task_evt is not None
    task_id = task_evt["task_id"]

    consumer = redis.Redis.from_url(redis_url, decode_responses=True)
    entries = consumer.xrange("floorwatch:motion", "-", "+")
    assert len(entries) == 2  # ready event isn't on the stream, only the two pose_motion events

    # First frame has no baseline (active=False); second is the real signal.
    for _entry_id, fields in entries:
        raw = json.loads(fields["data"])
        engine.record_motion(raw["camera_id"], bool(raw.get("active")))

    # No time has actually elapsed yet on this task (record_motion calls
    # happened back-to-back) — let real time pass so elapsed_ratio clears
    # the (zeroed) grace period and the nudge condition can evaluate.
    time.sleep(0.5)
    out_events = engine.tick()
    nudges = [e for e in out_events if e["event_type"] == "task_low_effort_nudge"]
    assert len(nudges) == 1
    assert nudges[0]["shadow_mode_suppressed"] is True  # never actually sent

    # ── 4. completion flags since active time never meaningfully accrued ──
    evt = engine.complete_task(task_id)
    assert evt["event_type"] == "task_flag"
    digest_entries = digest.read_all()
    assert any(e["event_type"] == "task_flag" for e in digest_entries), \
        "flag was not written to the shift-digest store"

    server.shutdown()
