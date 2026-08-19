"""Tests for main.py's CCTV-driven auto-assignment (task workflow spec
flow 1: "task should be assigned automatically from CCTV monitor" ->
"assigned to one person from department"). Drives a real zone_gap event
through Redis with the Tier 1/2 timers sped way up so the zone actually
reaches Tier 3 (zone_escalated) within the test — same real-timer-plus-
polling approach as test_integration.py's
test_motion_stream_drives_active_time_and_completion_resolves, chosen
over calling main.py's async internals directly: those internals use
cluster_redis, whose connections are bound to the TestClient's own
background event loop, so awaiting them from a separate pytest-asyncio
test loop deadlocks instead of erroring — a mistake made once already
while writing this file."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
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
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(config, "NUDGE_TIMER_SECONDS", 0.15)
    monkeypatch.setattr(config, "COMMAND_TIMER_SECONDS", 0.15)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "EMPLOYEE_DIRECTORY_PATH", tmp_path / "employee_directory.json")
    monkeypatch.setattr(config, "TASK_STORE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")

    sys.modules.pop("main", None)
    sys.modules.pop("engine", None)
    sys.modules.pop("effort_engine", None)
    import main as main_module

    main_module.users.create_user("test-supervisor", "test-password-123", role="supervisor")

    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        login_resp = client.post("/api/login", json={"username": "test-supervisor", "password": "test-password-123"})
        token = login_resp.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        client.auth_token = token
        yield client, main_module, fake_redis_url


def _publish_gap(fake_redis_url, main_module, zone_id="theatre3", role_tag="janitor", camera_id="theatre3_cam_1"):
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)
    publisher.xadd(main_module.config.REDIS_STREAM, {"data": json.dumps({
        "camera_id": camera_id, "zone_id": zone_id, "role_tag": role_tag,
        "entity_ref": None, "event_type": "zone_gap",
        "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
    })})


def _wait_for_escalation(main_module, zone_id="theatre3", timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        z = main_module.engine.zones.get(zone_id)
        if z is not None and z.status == "escalated":
            return True
        time.sleep(0.05)
    return False


def _wait_for_auto_task(main_module, zone_id="theatre3", timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = [t for t in main_module.effort_engine.tasks.values()
                 if t.zone_id == zone_id and t.task_type == "auto_coverage"]
        if tasks:
            return tasks
        time.sleep(0.05)
    return []


def test_auto_assign_picks_eligible_employee_in_department(app_client):
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15551230101")

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    tasks = _wait_for_auto_task(main_module)

    assert len(tasks) == 1
    assert tasks[0].assigned_to == "101"
    assert tasks[0].assigned_by == "system:auto_assign"


def test_auto_assign_picks_least_loaded_employee(app_client):
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15551230101")
    main_module.employee_directory.add("102", "Sam Rivera", "employee", "janitor", "+15551230102")
    # give 101 an existing open task so 102 is the less-loaded choice
    main_module.effort_engine.assign_task(
        "Existing task", "theatre3", 60, "clean_door", assigned_to="101", assigned_by="supervisor:test")

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    tasks = _wait_for_auto_task(main_module)

    assert len(tasks) == 1
    assert tasks[0].assigned_to == "102"


def test_auto_assign_never_picks_a_supervisor(app_client):
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("900", "Jordan Lee", "supervisor", "janitor", "+15551230900")

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    tasks = _wait_for_auto_task(main_module)

    assert len(tasks) == 1
    assert tasks[0].assigned_to is None  # no eligible employee — supervisors aren't auto-assigned coverage


def test_auto_assign_creates_unassigned_task_when_no_eligible_employee(app_client):
    client, main_module, fake_redis_url = app_client
    # no employees in the directory at all

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    tasks = _wait_for_auto_task(main_module)

    assert len(tasks) == 1
    assert tasks[0].assigned_to is None
    assert tasks[0].workflow_status == "unassigned"


def test_auto_assign_ignores_inactive_employees(app_client):
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15551230101")
    main_module.employee_directory.set_active("101", False)

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    tasks = _wait_for_auto_task(main_module)

    assert len(tasks) == 1
    assert tasks[0].assigned_to is None


def test_auto_assign_dedup_guard_skips_duplicate_coverage_task(app_client):
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15551230101")

    _publish_gap(fake_redis_url, main_module)
    assert _wait_for_escalation(main_module)
    assert _wait_for_auto_task(main_module)

    # zone stays "escalated" and keeps ticking — auto-assign must not fire again
    time.sleep(0.5)
    tasks = [t for t in main_module.effort_engine.tasks.values() if t.task_type == "auto_coverage"]
    assert len(tasks) == 1


def test_auto_assign_rejected_on_unstaffed_zone_creates_no_task(app_client, tmp_path):
    """An unstaffed zone never even reaches zone_gap processing (Global
    Constraint 5 — see engine.py's process_detection_event), so it can't
    reach zone_escalated at all; this confirms the gap stays completely
    silent end-to-end rather than somehow still spawning a coverage task."""
    client, main_module, fake_redis_url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15551230101")
    unstaffed_roster = tmp_path / "roster.json"
    unstaffed_roster.write_text(json.dumps({"theatre3": False}))
    main_module.roster.roster_path = unstaffed_roster

    _publish_gap(fake_redis_url, main_module)
    assert not _wait_for_escalation(main_module, timeout=1)  # never escalates — stays "covered"
    time.sleep(0.3)  # give auto-assign a fair chance to (incorrectly) fire anyway

    assert not any(t.zone_id == "theatre3" for t in main_module.effort_engine.tasks.values())
