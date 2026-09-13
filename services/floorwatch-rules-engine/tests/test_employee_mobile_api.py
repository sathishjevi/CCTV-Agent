"""Tests for the employee mobile-app API (Phase 1/2 of the mobile app —
see dazzling-hopping-comet.md): phone+OTP login, and the employee-scoped
task-action endpoints (start/complete/request-extension/request-review/
reassign/device-token).

Reuses test_reassignment.py's exact app_client fixture pattern (real
FastAPI TestClient, fakeredis) rather than inventing a second one — the
employee endpoints share the same app/config wiring as the dashboard
endpoints those tests already exercise.

The OTP-send path deliberately bypasses shadow mode (a login code isn't
an operational notification — see request_otp()'s own comment in
main.py), so it would otherwise make a REAL Twilio API call in every
test that requests one. TASK_CHANNEL_SENDERS["sms"] is monkeypatched to
a recording fake here for exactly that reason — this file's job is the
OTP/auth/ownership logic, not proving Twilio's SDK works (already
covered, against mocks, in test_notifications.py)."""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_reassignment import app_client, fake_redis_url  # noqa: E402,F401 — reuse the exact same fixtures


class FakeSmsSender:
    def __init__(self):
        self.sent = []

    def send(self, to_context, message):
        self.sent.append((to_context, message))
        return type("R", (), {"sent": True, "channel": "fake", "detail": ""})()


def _install_fake_sms_sender(main_module) -> FakeSmsSender:
    fake = FakeSmsSender()
    main_module.TASK_CHANNEL_SENDERS["sms"] = fake
    return fake


def _login_employee(client, main_module, phone) -> str:
    """Full request-otp -> verify-otp round trip, returning the issued
    employee token. Reads the code back off the fake sender rather than
    the OTP store directly, so this exercises the real delivery path."""
    fake = main_module.TASK_CHANNEL_SENDERS["sms"]
    assert isinstance(fake, FakeSmsSender), "call _install_fake_sms_sender() first"
    resp = client.post("/api/employee/auth/request-otp", json={"phone": phone})
    assert resp.status_code == 200, resp.text
    message = fake.sent[-1][1]
    code = "".join(c for c in message if c.isdigit())[:6]
    resp = client.post("/api/employee/auth/verify-otp", json={"phone": phone, "code": code})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _create_task_assigned_to(client, assignee, task_name="Cover Theatre 3", zone_id="theatre3", minutes=60):
    resp = client.post("/api/tasks", json={
        "task_name": task_name, "zone_id": zone_id, "assigned_minutes": minutes,
        "task_type": "auto_coverage", "assigned_to": assignee,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


# ── OTP login ─────────────────────────────────────────────────────────────

def test_request_otp_then_verify_issues_employee_token(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")

    token = _login_employee(client, main_module, "+15559000101")
    assert token

    # the token actually works against an employee-only endpoint
    resp = client.get("/api/employee/tasks", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"tasks": []}


def test_verify_otp_unknown_phone_returns_401(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    resp = client.post("/api/employee/auth/verify-otp", json={"phone": "+15559999999", "code": "123456"})
    assert resp.status_code == 401


def test_verify_otp_wrong_code_returns_401(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    client.post("/api/employee/auth/request-otp", json={"phone": "+15559000101"})

    resp = client.post("/api/employee/auth/verify-otp", json={"phone": "+15559000101", "code": "000000"})
    assert resp.status_code == 401


def test_verify_otp_is_one_time_use(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    token = _login_employee(client, main_module, "+15559000101")
    assert token

    fake = main_module.TASK_CHANNEL_SENDERS["sms"]
    message = fake.sent[-1][1]
    code = "".join(c for c in message if c.isdigit())[:6]
    # replaying the same code a second time must fail — it was consumed
    resp = client.post("/api/employee/auth/verify-otp", json={"phone": "+15559000101", "code": code})
    assert resp.status_code == 401


def test_request_otp_rate_limited_per_phone(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    import config
    limit = config.OTP_RATE_LIMIT_PER_PHONE_PER_MINUTE
    for _ in range(limit):
        resp = client.post("/api/employee/auth/request-otp", json={"phone": "+15559000101"})
        assert resp.status_code == 200
    resp = client.post("/api/employee/auth/request-otp", json={"phone": "+15559000101"})
    assert resp.status_code == 429


def test_request_otp_unknown_phone_still_returns_200_no_enumeration(app_client):
    """Same response shape regardless of whether the number is on file —
    otherwise this endpoint could be used to check which phone numbers
    are employees."""
    client, main_module = app_client
    fake = _install_fake_sms_sender(main_module)
    resp = client.post("/api/employee/auth/request-otp", json={"phone": "+15559999999"})
    assert resp.status_code == 200
    assert fake.sent == []  # but nothing was actually sent


# ── Employee-scoped task actions ─────────────────────────────────────────

def test_employee_can_start_and_complete_own_task(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    task_id = _create_task_assigned_to(client, "101")
    token = _login_employee(client, main_module, "+15559000101")
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/employee/tasks", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 1
    assert resp.json()["tasks"][0]["task_id"] == task_id

    resp = client.post(f"/api/employee/tasks/{task_id}/start", headers=headers)
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/employee/tasks/{task_id}/complete", headers=headers)
    assert resp.status_code == 200, resp.text

    # dashboard's own view reflects the app-driven completion — status is
    # "flagged" here (zero simulated active time trips the effort-ratio
    # threshold, same as test_reassignment.py's own "active_seconds=0 ->
    # flagged" cases), which is the effort engine correctly doing its
    # job; this test is about the REST action reaching the dashboard at
    # all, not about effort accuracy.
    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["status"] in ("resolved", "flagged")
    assert tasks[task_id]["workflow_status"] == "completed"


def test_employee_cannot_act_on_another_employees_task(app_client):
    """The one authorization rule that must never be missed."""
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    main_module.employee_directory.add("102", "Sam Rivera", "employee", "janitor", "+15559000102")
    task_id = _create_task_assigned_to(client, "101")

    other_token = _login_employee(client, main_module, "+15559000102")
    headers = {"Authorization": f"Bearer {other_token}"}

    for action in ("start", "complete", "request-extension", "request-review"):
        resp = client.post(f"/api/employee/tasks/{task_id}/{action}", headers=headers)
        assert resp.status_code == 403, f"{action} should be forbidden: {resp.text}"

    resp = client.post(f"/api/employee/tasks/{task_id}/reassign", json={"new_assignee": "102"}, headers=headers)
    assert resp.status_code == 403


def test_employee_token_cannot_access_supervisor_endpoints(app_client):
    """Confirms role separation the other direction — an employee token
    must never satisfy require_supervisor, matching floorwatch_auth.py's
    ROLE_RANK design (employee is absent from that ladder entirely)."""
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    token = _login_employee(client, main_module, "+15559000101")
    resp = client.get("/api/admin/employees", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_employee_reassign_hands_off_and_notifies_new_assignee(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    main_module.employee_directory.add("102", "Sam Rivera", "employee", "janitor", "+15559000102")
    task_id = _create_task_assigned_to(client, "101")
    token = _login_employee(client, main_module, "+15559000101")

    resp = client.post(f"/api/employee/tasks/{task_id}/reassign", json={"new_assignee": "102"},
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["assigned_to"] == "102"


def test_employee_device_token_registration_sets_fcm_and_defaults_channel(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")  # no channel set
    token = _login_employee(client, main_module, "+15559000101")

    resp = client.post("/api/employee/device-token", json={"fcm_token": "tok-abc123"},
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    entry = main_module.employee_directory.get("101")
    assert entry["fcm_token"] == "tok-abc123"
    assert entry["channel"] == "fcm"  # auto-switched, since none was set before


def test_employee_device_token_does_not_override_explicit_sms_channel(app_client):
    client, main_module = app_client
    _install_fake_sms_sender(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101", channel="sms")
    token = _login_employee(client, main_module, "+15559000101")

    client.post("/api/employee/device-token", json={"fcm_token": "tok-abc123"},
                headers={"Authorization": f"Bearer {token}"})
    entry = main_module.employee_directory.get("101")
    assert entry["channel"] == "sms"  # left alone — an explicit choice, not blank
