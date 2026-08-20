"""Tests for the reassignment/delegation half of the task workflow
(Conflict 2's resolution — supervisor REST endpoint + employee SMS
keyword, sharing effort_engine.reassign_task()), and for Feature 1's
per-employee notification channel routing.

SMS-driven actions are exercised through the REAL webhook route
(client.post to /api/webhooks/twilio-sms) rather than by awaiting
main.py's async command handlers directly from the test — those handlers
use cluster_redis, whose connections are bound to the TestClient's own
background event loop/thread; awaiting them from a separate pytest test
function's loop deadlocks instead of erroring (see test_auto_assignment.py's
module docstring — the same mistake, avoided here from the start).
Signature validation itself is monkeypatched to always pass, since
Twilio's HMAC algorithm is already covered by test_sms_webhook.py's
mocked-validator tests; this file's job is the routing/audit-trail logic
behind the webhook, not Twilio's crypto."""

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
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://floorwatch.example.test")
    # A realistic "channel mostly configured" pilot state — Feature 1's
    # own default-mapping test below overrides this per-test as needed.
    monkeypatch.setattr(config, "NOTIFY_CHANNEL", "twilio")

    sys.modules.pop("main", None)
    sys.modules.pop("engine", None)
    sys.modules.pop("effort_engine", None)
    import main as main_module
    main_module.validate_signature = lambda *a, **kw: True  # see module docstring

    main_module.users.create_user("test-supervisor", "test-password-123", role="supervisor")

    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        login_resp = client.post("/api/login", json={"username": "test-supervisor", "password": "test-password-123"})
        token = login_resp.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        client.auth_token = token
        yield client, main_module


def _sms(client, main_module, phone, body):
    return client.post("/api/webhooks/twilio-sms", data={"From": phone, "Body": body},
                        headers={"X-Twilio-Signature": "irrelevant-mocked"})


def _create_task_assigned_to(client, assignee, task_name="Cover Theatre 3", zone_id="theatre3", minutes=60):
    resp = client.post("/api/tasks", json={
        "task_name": task_name, "zone_id": zone_id, "assigned_minutes": minutes,
        "task_type": "auto_coverage", "assigned_to": assignee,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


def _history(client, task_id):
    return client.get("/api/history", params={"task_id": task_id}).json()


# ── full audit-trail flow: auto-assigned supervisor -> REST delegation
# -> SMS delegation to a second line employee ────────────────────────────

def test_full_auto_assign_then_supervisor_reassign_then_employee_sms_reassign(app_client):
    client, main_module = app_client
    main_module.employee_directory.add(
        "900", "Jordan Lee", "supervisor", "janitor", "+15559000900", is_primary_contact=True)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    main_module.employee_directory.add("102", "Sam Rivera", "employee", "janitor", "+15559000102")

    # simulates the state left by auto-assignment (already covered
    # end-to-end in test_auto_assignment.py) — created directly here so
    # this test stays focused on what happens AFTER that point.
    task_id = _create_task_assigned_to(client, "900")

    # supervisor delegates to a LINE EMPLOYEE via the dashboard REST endpoint
    resp = client.post(f"/api/tasks/{task_id}/reassign", json={"new_assignee": "101"})
    assert resp.status_code == 200, resp.text

    history = _history(client, task_id)
    reassign_events = [e for e in history if e["event_type"] == "task_reassigned"]
    assert len(reassign_events) == 1
    assert reassign_events[0]["previous_assignee"] == "900"
    assert reassign_events[0]["assigned_to"] == "101"
    assert reassign_events[0]["reassigned_by"] == "supervisor:test-supervisor"

    # the employee now assigned hands it off again via SMS
    sms_resp = _sms(client, main_module, "+15559000101", "REASSIGN 102")
    assert sms_resp.status_code == 200
    assert "handed off to employee 102" in sms_resp.text

    history = _history(client, task_id)
    reassign_events = [e for e in history if e["event_type"] == "task_reassigned"]
    assert len(reassign_events) == 2
    second = reassign_events[-1] if reassign_events[0]["previous_assignee"] == "900" else reassign_events[0]
    assert second["previous_assignee"] == "101"
    assert second["assigned_to"] == "102"
    assert second["reassigned_by"] == "employee:101"

    # nothing was deleted — the original assignment is still in the log
    assert any(e["event_type"] == "task_assigned" for e in history)
    tasks = client.get("/api/tasks").json()
    assert tasks[task_id]["assigned_to"] == "102"


def test_rest_reassign_allows_delegation_to_line_employee_not_just_supervisors(app_client):
    client, main_module = app_client
    main_module.employee_directory.add("900", "Jordan Lee", "supervisor", "janitor", "+15559000900")
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    task_id = _create_task_assigned_to(client, "900")

    resp = client.post(f"/api/tasks/{task_id}/reassign", json={"new_assignee": "101"})
    assert resp.status_code == 200
    assert client.get("/api/tasks").json()[task_id]["assigned_to"] == "101"


def test_rest_reassign_rejects_nonexistent_employee(app_client):
    client, main_module = app_client
    main_module.employee_directory.add("900", "Jordan Lee", "supervisor", "janitor", "+15559000900")
    task_id = _create_task_assigned_to(client, "900")

    resp = client.post(f"/api/tasks/{task_id}/reassign", json={"new_assignee": "ghost"})
    assert resp.status_code == 400
    assert "not found" in resp.json()["error"]
    assert client.get("/api/tasks").json()[task_id]["assigned_to"] == "900"  # unchanged


def test_rest_reassign_rejects_inactive_employee(app_client):
    client, main_module = app_client
    main_module.employee_directory.add("900", "Jordan Lee", "supervisor", "janitor", "+15559000900")
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    main_module.employee_directory.set_active("101", False)
    task_id = _create_task_assigned_to(client, "900")

    resp = client.post(f"/api/tasks/{task_id}/reassign", json={"new_assignee": "101"})
    assert resp.status_code == 400
    assert "inactive" in resp.json()["error"]


def test_sms_reassign_rejects_unknown_target(app_client):
    client, main_module = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    task_id = _create_task_assigned_to(client, "101")

    resp = _sms(client, main_module, "+15559000101", "REASSIGN ghost")
    assert "not found or inactive" in resp.text
    assert client.get("/api/tasks").json()[task_id]["assigned_to"] == "101"  # unchanged


def test_sms_reassign_without_target_returns_usage_reply(app_client):
    client, main_module = app_client
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")
    _create_task_assigned_to(client, "101")

    resp = _sms(client, main_module, "+15559000101", "REASSIGN")
    assert "Reply REASSIGN" in resp.text


def test_sms_reassign_from_unrecognized_number_is_rejected(app_client):
    client, main_module = app_client
    resp = _sms(client, main_module, "+19998887777", "REASSIGN 101")
    assert "isn't recognized" in resp.text


# ── Feature 1 — per-employee notification channel ───────────────────────

class _RecordingSender:
    """Mimics TwilioSmsSender/FcmSender's real contract (only reads its
    own required field, reports a clear failure if missing) without
    needing real Twilio/Firebase credentials — substituted into
    main_module.TASK_CHANNEL_SENDERS so these tests check ROUTING, not
    the already-covered (test_notifications.py) sender internals."""

    def __init__(self, required_key, channel_name):
        self.required_key = required_key
        self.channel_name = channel_name
        self.calls = []

    def send(self, to_context, message):
        self.calls.append((dict(to_context), message))
        from notifications import NotificationResult
        value = to_context.get(self.required_key)
        if not value:
            return NotificationResult(sent=False, channel=self.channel_name,
                                       detail=f"no {self.required_key} on file")
        return NotificationResult(sent=True, channel=self.channel_name, detail="ok")


def _install_recording_senders(main_module):
    sms_sender = _RecordingSender("phone", "twilio_sms")
    fcm_sender = _RecordingSender("fcm_token", "fcm")
    main_module.TASK_CHANNEL_SENDERS = {"sms": sms_sender, "fcm": fcm_sender}
    main_module.config.SHADOW_MODE = False  # so _send_task_notification actually reaches the sender
    return sms_sender, fcm_sender


def test_channel_routes_sms_employee_to_twilio_sender(app_client):
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101", channel="sms")

    result = main_module._send_task_notification("101", "hello")

    assert result["sent"] is True
    assert len(sms_sender.calls) == 1
    assert sms_sender.calls[0][0]["phone"] == "+15559000101"
    assert len(fcm_sender.calls) == 0


def test_channel_routes_fcm_employee_to_fcm_sender(app_client):
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    main_module.employee_directory.add(
        "101", "Alex Chen", "employee", "janitor", "+15559000101",
        channel="fcm", fcm_token="device-token-abc")

    result = main_module._send_task_notification("101", "hello")

    assert result["sent"] is True
    assert len(fcm_sender.calls) == 1
    assert fcm_sender.calls[0][0]["fcm_token"] == "device-token-abc"
    assert len(sms_sender.calls) == 0


def test_channel_missing_fcm_token_fails_loudly_without_falling_back_to_sms(app_client):
    """Explicit requirement: a contact configured for fcm but missing
    the fcm_token must fail cleanly and NOT silently retry over SMS
    (which could go to a stale/wrong number)."""
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    main_module.employee_directory.add(
        "101", "Alex Chen", "employee", "janitor", "+15559000101", channel="fcm")  # no fcm_token

    result = main_module._send_task_notification("101", "hello")

    assert result["sent"] is False
    assert result["channel"] == "fcm"
    assert "no fcm_token on file" in result["detail"]
    assert len(fcm_sender.calls) == 1  # the fcm sender WAS tried, and correctly reported the gap
    assert len(sms_sender.calls) == 0  # never silently fell back to SMS


def test_channel_missing_phone_for_sms_employee_fails_loudly(app_client):
    """Same requirement, the other direction. validate_phone() at the
    REST layer normally prevents an empty phone from ever being stored,
    but the store's own add() (called directly here, bypassing that
    REST-layer check) has no such guard — confirming the ROUTING layer
    itself never guesses a different channel even if the delivery
    detail were somehow absent, regardless of how it got that way."""
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "", channel="sms")

    result = main_module._send_task_notification("101", "hello")
    assert result["sent"] is False
    assert len(fcm_sender.calls) == 0  # no fallback attempt


def test_channel_defaults_to_global_notify_channel_mapping_when_unset(app_client):
    """Backward compatibility: an employee record added before Feature 1
    existed (no channel field) must keep behaving like the old
    always-SMS default, since the fixture's global NOTIFY_CHANNEL is
    "twilio" (-> "sms")."""
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    main_module.employee_directory.add("101", "Alex Chen", "employee", "janitor", "+15559000101")  # no channel

    result = main_module._send_task_notification("101", "hello")

    assert result["sent"] is True
    assert len(sms_sender.calls) == 1
    assert len(fcm_sender.calls) == 0


def test_channel_employee_override_wins_over_global_default(app_client):
    client, main_module = app_client
    sms_sender, fcm_sender = _install_recording_senders(main_module)
    # global default maps to "sms" (NOTIFY_CHANNEL="twilio" in the fixture),
    # but this employee explicitly overrides to fcm.
    main_module.employee_directory.add(
        "101", "Alex Chen", "employee", "janitor", "+15559000101",
        channel="fcm", fcm_token="device-token-xyz")

    main_module._send_task_notification("101", "hello")

    assert len(fcm_sender.calls) == 1
    assert len(sms_sender.calls) == 0
