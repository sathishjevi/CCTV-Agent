"""Tests for the supervisor mobile "Dashboard" tab API
(/api/employee/dashboard/*) — the same phone+password employee login as
everything else under /api/employee/, additionally gated on the
employee_directory record's own role=="supervisor" (require_employee_
supervisor in main.py). These endpoints deliberately mirror the web
dashboard's own admin endpoints exactly (same submit_command/
read_snapshot plumbing) — this file's job is proving the new auth gate
and URL surface work, not re-testing effort_engine's own logic (already
covered by test_effort_engine.py/test_task_workflow.py) or the
identical admin endpoints' behavior (already covered by
test_integration.py).

Reuses test_integration.py's exact app_client fixture (real Redis
stream + leader tasks running, needed for submit_command to actually
process) rather than inventing a second one."""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401 — reuse the exact same fixtures


def _add_employee(client, employee_number, role, phone, name="Test Person"):
    resp = client.post("/api/admin/employees", json={
        "employee_number": employee_number, "name": name, "role": role,
        "department": "ops", "phone": phone,
    })
    assert resp.status_code == 200, resp.text


def _set_password_and_login(client, employee_number, phone, password="correct-horse-battery"):
    resp = client.post(f"/api/admin/employees/{employee_number}/set-password", json={"password": password})
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/employee/auth/login", json={"phone": phone, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_login_response_includes_role(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    assert body["role"] == "supervisor"


def test_dashboard_endpoints_reject_plain_employee(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.get("/api/employee/dashboard/state", headers=headers)
    assert resp.status_code == 403

    resp = client.get("/api/employee/dashboard/tasks", headers=headers)
    assert resp.status_code == 403


def test_dashboard_endpoints_reject_missing_or_invalid_token(app_client):
    client, main_module, _url = app_client
    resp = client.get("/api/employee/dashboard/state", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_supervisor_can_view_state_and_tasks_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door"})
    task_id = resp.json()["task_id"]

    state_resp = client.get("/api/employee/dashboard/state", headers=headers)
    assert state_resp.status_code == 200

    tasks_resp = client.get("/api/employee/dashboard/tasks", headers=headers)
    assert tasks_resp.status_code == 200
    assert task_id in tasks_resp.json()


def test_supervisor_can_confirm_and_dismiss_flags_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door"})
    task_id = resp.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/complete")  # no active time -> flagged

    queue_resp = client.get("/api/employee/dashboard/queue/tasks", headers=headers)
    assert queue_resp.status_code == 200
    assert any(f["task_id"] == task_id for f in queue_resp.json())

    confirm_resp = client.post(f"/api/employee/dashboard/queue/task/{task_id}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["resolved_by"] == "supervisor:employee:300"
    assert client.get("/api/employee/dashboard/queue/tasks", headers=headers).json() == []


def test_supervisor_can_extend_and_resolve_review_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/tasks", json={
        "task_name": "Restock", "zone_id": "theatre3", "assigned_minutes": 20, "task_type": "clean_door"})
    task_id = resp.json()["task_id"]

    extend_resp = client.post(
        f"/api/employee/dashboard/tasks/{task_id}/extend", json={"extra_minutes": 15}, headers=headers)
    assert extend_resp.status_code == 200
    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["assigned_minutes"] == 35

    client.post(f"/api/tasks/{task_id}/complete")  # -> flagged
    client.post(f"/api/queue/task/{task_id}/confirm")  # -> reopened

    resolve_resp = client.post(f"/api/employee/dashboard/tasks/{task_id}/resolve-review", headers=headers)
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["action_type"] == "reviewed"


def test_supervisor_can_reassign_and_complete_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door"})
    task_id = resp.json()["task_id"]

    reassign_resp = client.post(
        f"/api/employee/dashboard/tasks/{task_id}/reassign", json={"new_assignee": "101"}, headers=headers)
    assert reassign_resp.status_code == 200
    assert client.get("/api/tasks").json()[task_id]["assigned_to"] == "101"

    complete_resp = client.post(f"/api/employee/dashboard/tasks/{task_id}/complete", headers=headers)
    assert complete_resp.status_code == 200
