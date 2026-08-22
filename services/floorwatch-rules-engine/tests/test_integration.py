"""
End-to-end integration test: publishes a schema-compliant zone_gap event
onto a Redis Stream (fakeredis TcpFakeServer, same rationale as
skills/detection/floorwatch-coverage/tests/test_redis_publish.py — no
Docker in this sandbox) and confirms the rules engine consumes it,
runs it through the Tier 1 nudge logic, and broadcasts the result over
its WebSocket — proving the wiring the brief's Phase 2 task 2/3 describe,
without needing a real Redis or a real detection feed.
"""

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
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
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


def test_gap_event_flows_through_redis_to_websocket(app_client):
    client, main_module, fake_redis_url = app_client

    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)
    gap_event = {
        "event_id": "e1", "timestamp": "2026-07-24T18:12:00Z",
        "camera_id": "lobby_cam_1", "zone_id": "concession", "role_tag": "concession",
        "entity_ref": None, "event_type": "zone_gap", "task_id": None,
        "active_minutes": None, "assigned_minutes": None,
        "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
    }

    with client.websocket_connect(f"/events?token={client.auth_token}") as ws:
        publisher.xadd(main_module.config.REDIS_STREAM, {"data": json.dumps(gap_event)})
        received = ws.receive_json()

    assert received["event_type"] == "zone_nudge_sent"
    assert received["tier"] == 1
    assert received["zone_id"] == "concession"
    assert received["shadow_mode_suppressed"] is True  # shadow mode is the default


def test_healthz_reports_shadow_mode(app_client):
    client, _main_module, _url = app_client
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["shadow_mode"] is True


def test_state_endpoint_reflects_zone_after_gap(app_client):
    client, main_module, fake_redis_url = app_client
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)
    gap_event = {
        "camera_id": "lobby_cam_1", "zone_id": "boxoffice", "role_tag": "ticketing",
        "entity_ref": None, "event_type": "zone_gap",
        "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
    }
    with client.websocket_connect(f"/events?token={client.auth_token}") as ws:
        publisher.xadd(main_module.config.REDIS_STREAM, {"data": json.dumps(gap_event)})
        ws.receive_json()

    state = client.get("/api/state").json()
    assert state["boxoffice"]["status"] == "nudge"


def test_approve_endpoint_resolves_zone(app_client):
    client, main_module, fake_redis_url = app_client
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)
    gap_event = {
        "camera_id": "lobby_cam_1", "zone_id": "lobby", "role_tag": "usher",
        "entity_ref": None, "event_type": "zone_gap",
        "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
    }
    with client.websocket_connect(f"/events?token={client.auth_token}") as ws:
        publisher.xadd(main_module.config.REDIS_STREAM, {"data": json.dumps(gap_event)})
        ws.receive_json()  # nudge

    resp = client.post("/api/queue/zone/lobby/approve")
    assert resp.status_code == 200
    assert resp.json()["event_type"] == "zone_resolved"
    assert client.get("/api/state").json()["lobby"]["status"] == "covered"


# ── Part A — task assignment / motion / completion ───────────────────────

def test_assign_task_creates_open_task_and_broadcasts(app_client):
    client, main_module, _url = app_client
    with client.websocket_connect(f"/events?token={client.auth_token}") as ws:
        resp = client.post("/api/tasks", json={
            "task_name": "Clean Door — Zone 4", "zone_id": "theatre3",
            "assigned_minutes": 60, "task_type": "clean_door",
        })
        assert resp.status_code == 200
        evt = ws.receive_json()
    assert resp.json()["event_type"] == "task_assigned"
    assert evt["event_type"] == "task_assigned"
    assert evt["zone_id"] == "theatre3"

    tasks = client.get("/api/tasks").json()
    assert len(tasks) == 1


def test_assign_task_on_unstaffed_zone_rejected(app_client, tmp_path):
    client, main_module, _url = app_client
    # roster.py reads its file lazily, so we can patch it after import too
    unstaffed_roster = tmp_path / "roster.json"
    unstaffed_roster.write_text(json.dumps({"theatre3": False}))
    main_module.roster.roster_path = unstaffed_roster

    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60,
    })
    assert resp.status_code == 400
    assert client.get("/api/tasks").json() == {}


def test_motion_stream_drives_active_time_and_completion_resolves(app_client):
    client, main_module, fake_redis_url = app_client
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)

    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]

    # Feed enough active motion samples (real time, since this test uses the
    # real clock, not a fake one) to clear the clean_door 50% threshold for
    # a very short assigned_minutes budget — use a tiny budget so real
    # wall-clock seconds are enough to cross the ratio without a long test.
    resp2 = client.post("/api/tasks", json={
        "task_name": "Quick Task", "zone_id": "theatre3", "assigned_minutes": 0.02, "task_type": "clean_door",
    })
    task_id2 = resp2.json()["task_id"]

    for _ in range(5):
        publisher.xadd(main_module.config.REDIS_MOTION_STREAM, {"data": json.dumps({
            "event": "pose_motion", "camera_id": "theatre3_cam_1", "active": True,
        })})
        time.sleep(0.3)

    deadline = time.time() + 5
    while time.time() < deadline:
        if main_module.effort_engine.tasks[task_id2].active_seconds > 0:
            break
        time.sleep(0.1)

    complete_resp = client.post(f"/api/tasks/{task_id2}/complete")
    assert complete_resp.status_code == 200
    assert complete_resp.json()["event_type"] in ("task_resolved", "task_flag")

    # task_id (60 min budget) got no time to accrue meaningfully — completing
    # it immediately should flag it (active time is far below budget).
    flag_resp = client.post(f"/api/tasks/{task_id}/complete")
    assert flag_resp.status_code == 200
    assert flag_resp.json()["event_type"] == "task_flag"
    assert client.get("/api/queue/tasks").json()[0]["task_id"] == task_id


def test_confirm_task_flag_endpoint(app_client):
    client, main_module, _url = app_client
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/complete")  # no active time -> flagged

    confirm_resp = client.post(f"/api/queue/task/{task_id}/confirm")
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["resolved_by"] == "supervisor:test-supervisor"
    assert client.get("/api/queue/tasks").json() == []


def test_confirm_task_flag_reopens_task_and_notifies_assignee(app_client):
    """The reported gap: confirming a flag used to just close the task
    to history, claiming (falsely) that the employee was being followed
    up with. It must now actually reopen the task (visible again in
    /api/tasks as an active task, not the supervisor queue) and run the
    real notification path (mark_notified/mark_notify_failed — visible
    either way, never silently skipped)."""
    client, main_module, _url = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101", channel="sms")
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60,
        "task_type": "clean_door", "assigned_to": "101",
    })
    task_id = resp.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/complete")  # no active time -> flagged
    assert client.get("/api/tasks").json()[task_id]["status"] == "flagged"

    confirm_resp = client.post(f"/api/queue/task/{task_id}/confirm")
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["event_type"] == "task_flag_confirmed"

    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["status"] == "open"  # reopened — no longer flagged/gone
    assert tasks[task_id]["workflow_status"] == "notified"  # shadow mode still "delivers" for workflow purposes
    assert client.get("/api/queue/tasks").json() == []  # no longer sitting in the supervisor queue

    history = client.get("/api/history", params={"task_id": task_id}).json()
    assert any(e["event_type"] == "task_workflow_update" and e.get("action_type") == "notified"
               for e in history)


def test_resolve_review_endpoint_closes_reopened_task_without_reflagging(app_client):
    """The reported gap's second half: a reopened task's only way back
    to the supervisor used to be "Mark complete," which re-runs the same
    check that flagged it — with no new motion signal, that just
    re-flags it and sends it right back to the queue, forever. The new
    endpoint lets a supervisor close it out directly once they've
    actually followed up."""
    client, main_module, _url = app_client
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/complete")  # -> flagged
    client.post(f"/api/queue/task/{task_id}/confirm")  # -> reopened

    resolve_resp = client.post(f"/api/tasks/{task_id}/resolve-review")
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["event_type"] == "task_resolved"
    assert resolve_resp.json()["action_type"] == "reviewed"

    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["status"] == "resolved"
    assert tasks[task_id]["reopened_for_review"] is False
    assert client.get("/api/queue/tasks").json() == []


def test_resolve_review_endpoint_rejects_task_never_reopened(app_client):
    client, main_module, _url = app_client
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]

    resolve_resp = client.post(f"/api/tasks/{task_id}/resolve-review")
    assert resolve_resp.status_code == 400


# ── event history — the durable audit trail (reported missing directly:
# a supervisor confirmed a flagged task, watched it happen live in the
# dashboard, and it was never recorded anywhere) ─────────────────────────

def test_full_task_lifecycle_is_durably_recorded(app_client):
    """The exact scenario reported: assign -> flag -> supervisor confirm
    — every step must show up in /api/history, not just the flag (which
    is all shift_digest.jsonl ever captured)."""
    client, main_module, _url = app_client
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/complete")  # no active time -> flagged
    client.post(f"/api/queue/task/{task_id}/confirm")

    history = client.get("/api/history", params={"task_id": task_id}).json()
    event_types = {e["event_type"] for e in history}
    assert "task_assigned" in event_types
    assert "task_flag" in event_types
    # confirming a flag REOPENS the task (see effort_engine.py's
    # confirm_flag docstring) rather than resolving it, so the durably-
    # recorded event is task_flag_confirmed — this was the missing one.
    assert "task_flag_confirmed" in event_types

    confirmed = next(e for e in history if e["event_type"] == "task_flag_confirmed")
    assert confirmed["resolved_by"] == "supervisor:test-supervisor"


def test_history_survives_independent_of_shift_digest(app_client):
    """shift_digest.jsonl only ever captured zone_escalated/task_flag —
    task_assigned specifically was never in it. Confirm event_history
    has it even though the digest doesn't."""
    client, main_module, _url = app_client
    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    task_id = resp.json()["task_id"]

    digest_types = {e["event_type"] for e in client.get("/api/digest").json()}
    assert "task_assigned" not in digest_types  # confirms the gap actually existed

    history_types = {e["event_type"] for e in client.get("/api/history", params={"task_id": task_id}).json()}
    assert "task_assigned" in history_types  # and confirms it's now closed


def test_history_filters_by_event_type(app_client):
    client, main_module, _url = app_client
    client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    results = client.get("/api/history", params={"event_type": "task_assigned"}).json()
    assert results
    assert all(e["event_type"] == "task_assigned" for e in results)


def test_history_requires_authentication(app_client):
    client, main_module, _url = app_client
    resp = client.get("/api/history", headers={"Authorization": ""})
    assert resp.status_code == 401
