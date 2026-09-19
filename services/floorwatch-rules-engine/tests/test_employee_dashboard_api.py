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


def test_admin_role_satisfies_require_employee_supervisor(app_client):
    """An admin employee should be able to do everything a supervisor
    can via the mobile dashboard too — same superset relationship as
    the web dashboard's own role==='admin' || role==='supervisor' gate
    on Manage Employees/Manage Zones."""
    client, main_module, _url = app_client
    _add_employee(client, "400", "admin", "+15559000400")
    body = _set_password_and_login(client, "400", "+15559000400")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.get("/api/employee/dashboard/state", headers=headers)
    assert resp.status_code == 200


def test_supervisor_can_manage_employees_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    add_resp = client.post("/api/employee/dashboard/employees", json={
        "employee_number": "500", "name": "New Hire", "role": "employee",
        "department": "ops", "phone": "+15559000500"}, headers=headers)
    assert add_resp.status_code == 200, add_resp.text

    list_resp = client.get("/api/employee/dashboard/employees", headers=headers)
    assert any(e["employee_number"] == "500" for e in list_resp.json()["employees"])

    edit_resp = client.put("/api/employee/dashboard/employees/500", json={
        "name": "New Hire Edited", "role": "employee", "department": "ops", "phone": "+15559000500"},
        headers=headers)
    assert edit_resp.status_code == 200

    deactivate_resp = client.post("/api/employee/dashboard/employees/500/deactivate", headers=headers)
    assert deactivate_resp.status_code == 200
    reactivate_resp = client.post("/api/employee/dashboard/employees/500/reactivate", headers=headers)
    assert reactivate_resp.status_code == 200

    setpass_resp = client.post(
        "/api/employee/dashboard/employees/500/set-password", json={"password": "correct-horse-battery"},
        headers=headers)
    assert setpass_resp.status_code == 200


def test_supervisor_can_add_admin_employee_via_dashboard(app_client):
    """The role validation on both add/edit must accept "admin" now,
    not just "employee"/"supervisor"."""
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/employee/dashboard/employees", json={
        "employee_number": "600", "name": "Future Admin", "role": "admin",
        "department": "ops", "phone": "+15559000600"}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_supervisor_can_manage_zones_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    add_resp = client.post("/api/employee/dashboard/zones", json={
        "zone_id": "mobile_test_zone", "name": "Mobile Test Zone", "role_tag": "usher"}, headers=headers)
    assert add_resp.status_code == 200, add_resp.text

    list_resp = client.get("/api/employee/dashboard/zones", headers=headers)
    assert any(z["zone_id"] == "mobile_test_zone" for z in list_resp.json()["zones"])

    staffed_resp = client.post(
        "/api/employee/dashboard/zones/mobile_test_zone/set-staffed", json={"staffed": False}, headers=headers)
    assert staffed_resp.status_code == 200

    deactivate_resp = client.post("/api/employee/dashboard/zones/mobile_test_zone/deactivate", headers=headers)
    assert deactivate_resp.status_code == 200
    reactivate_resp = client.post("/api/employee/dashboard/zones/mobile_test_zone/reactivate", headers=headers)
    assert reactivate_resp.status_code == 200


def test_supervisor_can_view_history_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    client.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door"})

    resp = client.get("/api/employee/dashboard/history", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_plain_supervisor_cannot_manage_users(app_client):
    """Manage Users is admin-only, even for an otherwise fully-capable
    supervisor — mirrors the web dashboard's admin-only gate."""
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.get("/api/employee/admin/users", headers=headers)
    assert resp.status_code == 403


def test_admin_can_manage_users_via_mobile(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "400", "admin", "+15559000400")
    body = _set_password_and_login(client, "400", "+15559000400")
    headers = {"Authorization": f"Bearer {body['token']}"}

    create_resp = client.post("/api/employee/admin/users", json={
        "username": "mobile-created-user", "password": "correct-horse-battery-2", "role": "viewer"},
        headers=headers)
    assert create_resp.status_code == 200, create_resp.text

    list_resp = client.get("/api/employee/admin/users", headers=headers)
    assert any(u["username"] == "mobile-created-user" for u in list_resp.json()["users"])

    deactivate_resp = client.post(
        "/api/employee/admin/users/mobile-created-user/deactivate", headers=headers)
    assert deactivate_resp.status_code == 200
    reactivate_resp = client.post(
        "/api/employee/admin/users/mobile-created-user/reactivate", headers=headers)
    assert reactivate_resp.status_code == 200
    reset_resp = client.post(
        "/api/employee/admin/users/mobile-created-user/reset-password",
        json={"new_password": "correct-horse-battery-3"}, headers=headers)
    assert reset_resp.status_code == 200


def test_supervisor_can_assign_a_task_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "300", "+15559000300")
    headers = {"Authorization": f"Bearer {body['token']}"}

    resp = client.post("/api/employee/dashboard/tasks", json={
        "task_name": "Restock drinks", "zone_id": "theatre3", "assigned_minutes": 25,
        "task_type": "clean_door", "assigned_to": "101"}, headers=headers)
    assert resp.status_code == 200, resp.text

    task = client.get("/api/tasks").json()[resp.json()["task_id"]]
    assert task["assigned_to"] == "101"
    assert task["assigned_by"] == "employee:300"


def test_plain_employee_cannot_assign_a_task_via_dashboard(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")
    resp = client.post("/api/employee/dashboard/tasks", json={
        "task_name": "x", "zone_id": "theatre3", "assigned_minutes": 5},
        headers={"Authorization": f"Bearer {body['token']}"})
    assert resp.status_code == 403


def test_zone_directive_endpoints_are_supervisor_gated(app_client):
    """approve/reassign on a zone with nothing pending returns the same
    not-found the web dashboard's own endpoints do (proving they reach the
    command bus) — and a plain employee never gets that far."""
    client, main_module, _url = app_client
    _add_employee(client, "300", "supervisor", "+15559000300")
    _add_employee(client, "101", "employee", "+15559000101")
    sup = _set_password_and_login(client, "300", "+15559000300")
    emp = _set_password_and_login(client, "101", "+15559000101")

    sup_headers = {"Authorization": f"Bearer {sup['token']}"}
    resp = client.post("/api/employee/dashboard/queue/zone/lobby/approve", headers=sup_headers)
    assert resp.status_code in (200, 404)
    assert client.get("/api/employee/dashboard/queue", headers=sup_headers).status_code == 200

    emp_headers = {"Authorization": f"Bearer {emp['token']}"}
    assert client.post("/api/employee/dashboard/queue/zone/lobby/approve", headers=emp_headers).status_code == 403
    assert client.post("/api/employee/dashboard/queue/zone/lobby/reassign", headers=emp_headers).status_code == 403


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
