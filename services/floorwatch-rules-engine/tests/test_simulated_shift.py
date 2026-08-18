"""
Unattended full-pipeline simulated-shift test — build brief Phase 4 task 2:
"Write an end-to-end integration test that runs the full pipeline (camera
feed -> DeepCamera -> Floorwatch skill -> rules engine -> dashboard ->
notification-send stub) unattended for a simulated shift, and asserts no
unhandled errors or dropped events."

Wires together every REAL piece built in Phases 1-4, not mocks:
  - skills/detection/floorwatch-coverage/scripts/main.py (Part B) and
    skills/detection/floorwatch-pose/scripts/pose.py (Part A) as actual
    subprocesses, fed synthetic frame/detection events standing in for
    "camera feed -> DeepCamera" (no real cameras in this sandbox — same
    limitation flagged since PHASE_1_NOTES.md).
  - A fakeredis TcpFakeServer standing in for Redis (no Docker here).
  - The real FastAPI rules engine app (both RulesEngine and EffortEngine),
    consuming both streams.
  - A real WebSocket client standing in for the dashboard, collecting
    broadcast events via a bounded, non-blocking-forever poll (see
    `_collect_available_events` — a plain blocking `receive_json()` loop
    has no timeout and can deadlock against the test session's close, so
    each attempt runs in its own throwaway daemon thread with a short
    join timeout instead of sharing one long-lived reader thread).
  - The real NoOpSender notification path (shadow mode stays on — this
    test is about pipeline robustness, not about actually validating a
    go-live's real send, which is test_notifications.py's job).

"No dropped events" is asserted concretely via Redis XPENDING on both
consumer groups: every event a producer (skill) published must end up
acknowledged by the rules engine's consumer loop, not stuck unread.
"No unhandled errors" is asserted by scanning both skill subprocesses'
stderr (collected via subprocess.communicate(timeout=...), never a bare
blocking .read()) for an uncaught Python traceback.
"""

import json
import queue as queue_mod
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-coverage" / "scripts" / "main.py"
POSE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-pose" / "scripts" / "pose.py"
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("PIL")
pytest.importorskip("fastapi")


@pytest.fixture
def fake_redis_url():
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


@pytest.fixture
def app_client(fake_redis_url, monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "REDIS_URL", fake_redis_url)
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "NUDGE_TIMER_SECONDS", 0.3)
    monkeypatch.setattr(config, "COMMAND_TIMER_SECONDS", 0.3)
    monkeypatch.setattr(config, "RESOLVE_COOLDOWN_SECONDS", 0.1)
    monkeypatch.setattr(config, "EFFORT_UPDATE_INTERVAL_SECONDS", 0.3)
    monkeypatch.setattr(config, "EFFORT_NUDGE_GRACE_RATIO", 0.0)
    monkeypatch.setattr(config, "EFFORT_NUDGE_MARGIN", 0.1)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")

    for mod in ("main", "engine", "effort_engine", "notifications"):
        sys.modules.pop(mod, None)
    import main as main_module

    main_module.users.create_user("test-supervisor", "test-password-123", role="supervisor")

    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        login_resp = client.post("/api/login", json={"username": "test-supervisor", "password": "test-password-123"})
        token = login_resp.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        client.auth_token = token
        yield client, main_module, fake_redis_url


def _make_image(path: Path, fill: int):
    from PIL import Image
    Image.new("L", (64, 64), color=fill).save(path)


def _collect_available_events(ws, max_wait: float = 2.0, per_attempt_timeout: float = 0.3) -> list:
    """Drains whatever's already queued on a TestClient WebSocket session
    without risking an indefinite block: `receive_json()` has no timeout
    parameter and a shared long-lived reader thread can deadlock against
    the session's close from another thread. Each attempt gets its own
    throwaway daemon thread with a short join timeout instead — if an
    attempt doesn't return in time, we assume the queue is drained and
    stop, abandoning that one stuck thread (harmless: it's a daemon, and
    the test process exits shortly after anyway)."""
    collected = []
    deadline = time.time() + max_wait
    while time.time() < deadline:
        result_queue: queue_mod.Queue = queue_mod.Queue(maxsize=1)

        def _attempt():
            try:
                result_queue.put(("ok", ws.receive_json()))
            except Exception as e:
                result_queue.put(("err", e))

        t = threading.Thread(target=_attempt, daemon=True)
        t.start()
        t.join(timeout=per_attempt_timeout)
        if t.is_alive():
            break  # nothing new arrived in time -> treat the queue as drained
        try:
            status, payload = result_queue.get_nowait()
        except queue_mod.Empty:
            break
        if status != "ok":
            break
        collected.append(payload)
    return collected


def _communicate_safely(proc: subprocess.Popen, timeout: float = 5.0) -> str:
    """Reads a subprocess's remaining stderr and waits for exit, bounded —
    never a bare blocking .read()/.wait()."""
    try:
        _stdout, stderr = proc.communicate(timeout=timeout)
        return stderr or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate(timeout=5)
        return (stderr or "") + "\n[test harness: had to force-kill subprocess after timeout]"


def test_unattended_simulated_shift_no_errors_no_dropped_events(app_client, tmp_path):
    client, main_module, redis_url = app_client

    # ── Set up a real zone calibration for the coverage skill ──────────
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    (zones_dir / "lobby_cam_1.json").write_text(json.dumps({
        "camera_id": "lobby_cam_1",
        "zones": [{"zone_id": "concession", "role_tag": "concession",
                    "polygon": [[100, 150], [400, 150], [400, 500], [100, 500]]}],
    }))

    # ── Spawn the two REAL upstream skills ──────────────────────────────
    coverage = subprocess.Popen(
        [sys.executable, str(COVERAGE_SCRIPT), "--zones-dir", str(zones_dir),
         "--debounce-seconds", "0.2", "--gap-confidence-threshold", "0.8",
         "--redis-url", redis_url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    coverage_ready = json.loads(coverage.stdout.readline())
    assert coverage_ready["redis_enabled"] is True

    pose = subprocess.Popen(
        [sys.executable, str(POSE_SCRIPT), "--model-path", str(tmp_path / "nonexistent.task"),
         "--active-threshold", "0.1", "--redis-url", redis_url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    pose_ready = json.loads(pose.stdout.readline())
    assert pose_ready["mode"] == "fallback"
    assert pose_ready["redis_enabled"] is True

    # ── Assign a Part A task in the same shift ──────────────────────────
    task_resp = client.post("/api/tasks", json={
        "task_name": "Sweep Theatre 3", "zone_id": "theatre3",
        "assigned_minutes": 0.02, "task_type": "lobby_sweep",
    })
    assert task_resp.status_code == 200
    task_id = task_resp.json()["task_id"]

    # ── Drive a simulated shift: alternating occupancy + motion frames ──
    # The dashboard-equivalent WS client connects BEFORE the shift starts
    # (matching reality — ConnectionManager.broadcast only reaches clients
    # connected at broadcast time, so connecting after the fact would miss
    # everything). There's still no live reader thread: we just leave the
    # connection open while driving frames synchronously in this thread,
    # and drain whatever queued up afterward via _collect_available_events
    # — avoiding any concurrent-thread-vs-close deadlock risk.
    rng = random.Random(1234)
    NUM_FRAMES = 20
    collected = []
    with client.websocket_connect(f"/events?token={client.auth_token}") as ws:
        for frame_id in range(1, NUM_FRAMES + 1):
            person_present = rng.random() > 0.35  # mostly present, some gaps
            objects = (
                [{"class": "person", "confidence": 0.9, "bbox": [150, 200, 250, 350]}]
                if person_present else []
            )
            coverage.stdin.write(json.dumps({
                "event": "detections", "frame_id": frame_id, "camera_id": "lobby_cam_1",
                "timestamp": f"2026-07-24T18:{frame_id:02d}:00Z", "objects": objects,
            }) + "\n")
            coverage.stdin.flush()

            fill = rng.choice([0, 255])  # alternating -> high motion signal sometimes
            img = tmp_path / f"frame_{frame_id}.jpg"
            _make_image(img, fill)
            pose.stdin.write(json.dumps({
                "event": "frame", "frame_id": frame_id, "camera_id": "theatre3_cam_1",
                "timestamp": f"2026-07-24T18:{frame_id:02d}:00Z", "frame_path": str(img),
            }) + "\n")
            pose.stdin.flush()

            time.sleep(0.03)

        # Let timers/ticks catch up (nudge/command timeouts, periodic
        # updates) before collecting — so events are already queued.
        time.sleep(2)
        collected = _collect_available_events(ws, max_wait=2.0)

    # Complete the Part A task to exercise that path too within the shift.
    complete_resp = client.post(f"/api/tasks/{task_id}/complete")
    assert complete_resp.status_code == 200

    coverage.stdin.write(json.dumps({"command": "stop"}) + "\n")
    coverage.stdin.flush()
    pose.stdin.write(json.dumps({"command": "stop"}) + "\n")
    pose.stdin.flush()
    coverage_stderr = _communicate_safely(coverage)
    pose_stderr = _communicate_safely(pose)

    # ── Assertion 1: no unhandled errors in either upstream skill ───────
    assert "Traceback (most recent call last)" not in coverage_stderr, coverage_stderr
    assert "Traceback (most recent call last)" not in pose_stderr, pose_stderr

    # ── Assertion 2: no dropped events — every published stream entry ──
    #    was consumed AND acknowledged by the rules engine.
    consumer = redis.Redis.from_url(redis_url, decode_responses=True)
    for stream, group in (
        (main_module.config.REDIS_STREAM, main_module.config.REDIS_CONSUMER_GROUP),
        (main_module.config.REDIS_MOTION_STREAM, main_module.config.REDIS_MOTION_CONSUMER_GROUP),
    ):
        published = consumer.xlen(stream)
        pending = consumer.xpending(stream, group)
        pending_count = pending["pending"] if isinstance(pending, dict) else pending[0]
        assert pending_count == 0, f"{pending_count} events left un-acked on {stream} ({published} published)"
        assert published > 0, f"expected the simulated shift to publish events onto {stream}"

    # ── Assertion 3: the dashboard-equivalent WS client actually saw a
    #    realistic mix of event types, not silence.
    event_types = {evt.get("event_type") for evt in collected}
    assert len(collected) > 0, "no events were broadcast to the dashboard WebSocket during the simulated shift"
    # RulesEngine only ever broadcasts actionable tier transitions, never
    # the raw zone_covered/zone_gap detection-layer signal (see
    # engine.py's process_detection_event) — so the dashboard-relevant
    # signal to look for is one of these.
    part_b_event_types = {"zone_nudge_sent", "zone_supervisor_command", "zone_escalated", "zone_resolved"}
    assert event_types & part_b_event_types, \
        f"expected at least one Part B tier transition, got {event_types}"
    part_a_event_types = {"task_assigned", "task_active_time_update", "task_low_effort_nudge",
                           "task_flag", "task_resolved"}
    assert event_types & part_a_event_types, \
        f"expected at least one Part A effort-tracking event, got {event_types}"

    # ── Assertion 4: the app's background loops are still alive (didn't
    #    silently die mid-shift, which would otherwise go unnoticed).
    health = client.get("/healthz").json()
    assert health["ok"] is True

    # ── Assertion 5: whatever escalations/flags happened were durably
    #    logged with well-formed shared-schema events.
    digest_entries = main_module.digest.read_all()
    for entry in digest_entries:
        assert "event_type" in entry and "zone_id" in entry and "timestamp" in entry
