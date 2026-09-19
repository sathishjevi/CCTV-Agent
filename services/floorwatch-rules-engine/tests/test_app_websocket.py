"""Tests for the mobile app's live-update channel (/ws/app) and for the
guard that keeps employee-app tokens off the web dashboard's full-event
socket (/events).

/ws/app only ever sends {"hint": "refresh", ...} — never the event — so the
important properties are WHO gets a hint (scoping) and that nothing else
leaks. Reuses test_integration.py's app_client fixture (real Redis stream +
leader loops, needed for events to actually broadcast)."""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401


def _employee_token(client, number, role, phone):
    resp = client.post("/api/admin/employees", json={
        "employee_number": number, "name": f"Person {number}", "role": role,
        "department": "ops", "phone": phone})
    assert resp.status_code == 200, resp.text
    client.post(f"/api/admin/employees/{number}/set-password", json={"password": "correct-horse-battery"})
    resp = client.post("/api/employee/auth/login", json={"phone": phone, "password": "correct-horse-battery"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _assign(client, assigned_to):
    resp = client.post("/api/tasks", json={
        "task_name": f"Task for {assigned_to}", "zone_id": "theatre3", "assigned_minutes": 30,
        "task_type": "clean_door", "assigned_to": assigned_to})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


# ── app_hint_for — the scoping rule itself ──────────────────────────────

def test_hint_never_contains_the_event_payload(app_client):
    _client, main_module, _url = app_client
    evt = {"event_type": "task_assigned", "task_id": "t1", "assigned_to": "101",
           "task_name": "SECRET NAME", "message": "SECRET MESSAGE"}
    hint = main_module.app_hint_for(evt, {"all": True})
    assert hint == {"hint": "refresh", "event_type": "task_assigned", "task_id": "t1"}


def test_employee_scope_only_matches_their_own_tasks(app_client):
    _client, main_module, _url = app_client
    mine = {"event_type": "task_workflow_update", "task_id": "t1", "assigned_to": "101"}
    theirs = {"event_type": "task_workflow_update", "task_id": "t2", "assigned_to": "202"}
    zone_only = {"event_type": "zone_gap", "zone_id": "lobby"}
    scope = {"employee": "101"}
    assert main_module.app_hint_for(mine, scope) is not None
    assert main_module.app_hint_for(theirs, scope) is None
    assert main_module.app_hint_for(zone_only, scope) is None


def test_previous_assignee_is_told_when_task_is_reassigned_away(app_client):
    _client, main_module, _url = app_client
    evt = {"event_type": "task_reassigned", "task_id": "t1", "assigned_to": "202", "previous_assignee": "101"}
    assert main_module.app_hint_for(evt, {"employee": "101"}) is not None
    assert main_module.app_hint_for(evt, {"employee": "303"}) is None


# ── the sockets ─────────────────────────────────────────────────────────

def test_events_socket_rejects_employee_app_tokens(app_client):
    """The full-event socket must not be reachable with an employee token —
    it would stream every task on the floor to any logged-in employee."""
    client, _main_module, _url = app_client
    token = _employee_token(client, "101", "employee", "+15559000101")
    with pytest.raises(Exception):
        with client.websocket_connect(f"/events?token={token}"):
            pass


def test_app_socket_rejects_bad_token(app_client):
    client, _main_module, _url = app_client
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/app?token=garbage"):
            pass


def test_app_socket_tells_an_employee_only_about_their_own_tasks(app_client):
    client, _main_module, _url = app_client
    token = _employee_token(client, "101", "employee", "+15559000101")
    _employee_token(client, "202", "employee", "+15559000202")

    with client.websocket_connect(f"/ws/app?token={token}") as ws:
        _assign(client, "202")            # someone else's — must NOT arrive
        mine = _assign(client, "101")     # theirs — first thing they should hear
        hint = ws.receive_json()

    assert hint["hint"] == "refresh"
    assert hint["task_id"] == mine


def test_app_socket_tells_a_supervisor_employee_about_everything(app_client):
    client, _main_module, _url = app_client
    token = _employee_token(client, "300", "supervisor", "+15559000300")
    _employee_token(client, "202", "employee", "+15559000202")

    with client.websocket_connect(f"/ws/app?token={token}") as ws:
        other = _assign(client, "202")
        hint = ws.receive_json()

    assert hint["hint"] == "refresh"
    assert hint["task_id"] == other


def test_app_socket_accepts_a_dashboard_account_token(app_client):
    client, _main_module, _url = app_client
    with client.websocket_connect(f"/ws/app?token={client.auth_token}") as ws:
        task_id = _assign(client, "")
        hint = ws.receive_json()
    assert hint["task_id"] == task_id
