"""Privacy: the public notice, per-employee acknowledgment, access/export,
the data-subject request register, erasure, and automatic retention."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401
from test_employee_dashboard_api import _add_employee, _set_password_and_login  # noqa: E402


def _bearer(body):
    return {"Authorization": f"Bearer {body['token']}"}


def _admin(client, main_module, name="priv-admin"):
    main_module.users.create_user(name, "correct-horse-battery-9", role="admin", must_change_password=False)
    token = client.post("/api/login", json={"username": name, "password": "correct-horse-battery-9"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _employee(client, number="104", phone="+15559000104", role="employee"):
    _add_employee(client, number, role, phone, name="Sam Rivera")
    return _set_password_and_login(client, number, phone)


# ── the notice ──────────────────────────────────────────────────────────────

def test_the_notice_is_public_and_needs_no_login(app_client):
    client, _m, _u = app_client
    resp = client.get("/api/privacy-notice", headers={"Authorization": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] and "Your rights" in body["text"]
    page = client.get("/privacy", headers={"Authorization": ""})
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<h2>Your rights</h2>" in page.text


def test_the_notice_flags_when_organisation_details_are_not_configured(app_client):
    client, main_module, _u = app_client
    assert client.get("/api/privacy-notice").json()["configured"] is False
    assert "[Organisation name" in client.get("/privacy").text


def test_the_notice_states_the_retention_periods_that_are_actually_configured(app_client, monkeypatch):
    client, main_module, _u = app_client
    monkeypatch.setattr(main_module.config, "EVENT_HISTORY_RETENTION_DAYS", 17)
    monkeypatch.setattr(main_module.config, "EMPLOYEE_RETENTION_DAYS", 123)
    text = client.get("/api/privacy-notice").json()["text"]
    assert "17 days" in text and "123 days" in text


def test_changing_what_the_system_does_changes_the_notice_version(app_client, monkeypatch):
    client, main_module, _u = app_client
    before = client.get("/api/privacy-notice").json()["version"]
    monkeypatch.setattr(main_module.config, "EVENT_HISTORY_RETENTION_DAYS", 999)
    assert client.get("/api/privacy-notice").json()["version"] != before


def test_vendors_appear_in_the_notice_only_when_they_are_in_use(app_client, monkeypatch):
    client, main_module, _u = app_client
    assert "PostHog" not in client.get("/api/privacy-notice").json()["text"]
    monkeypatch.setattr(main_module.config, "POSTHOG_API_KEY", "phc_x")
    monkeypatch.setattr(main_module.config, "PRIVACY_AI_ASSISTANT", True)
    text = client.get("/api/privacy-notice").json()["text"]
    assert "PostHog" in text and "Anthropic" in text


def test_the_notice_escapes_html_in_configured_values(app_client, monkeypatch):
    client, main_module, _u = app_client
    monkeypatch.setattr(main_module.config, "ORG_NAME", "<script>alert(1)</script>")
    monkeypatch.setattr(main_module.config, "PRIVACY_CONTACT", "privacy@example.com")
    assert "<script>alert(1)</script>" not in client.get("/privacy").text


# ── acknowledgment ──────────────────────────────────────────────────────────

def test_an_employee_must_acknowledge_the_current_notice_version(app_client):
    client, _m, _u = app_client
    body = _employee(client)
    status = client.get("/api/employee/me/privacy", headers=_bearer(body)).json()
    assert status["accepted"] is False and status["accepted_version"] is None

    ok = client.post("/api/employee/me/privacy-notice/accept", json={"version": status["notice_version"]},
                     headers=_bearer(body))
    assert ok.status_code == 200
    assert client.get("/api/employee/me/privacy", headers=_bearer(body)).json()["accepted"] is True


def test_acknowledging_an_old_version_is_refused(app_client):
    client, _m, _u = app_client
    body = _employee(client)
    resp = client.post("/api/employee/me/privacy-notice/accept", json={"version": "stale-version"}, headers=_bearer(body))
    assert resp.status_code == 409
    assert client.get("/api/employee/me/privacy", headers=_bearer(body)).json()["accepted"] is False


def test_a_new_notice_version_asks_everyone_again(app_client, monkeypatch):
    client, main_module, _u = app_client
    body = _employee(client)
    version = client.get("/api/employee/me/privacy", headers=_bearer(body)).json()["notice_version"]
    client.post("/api/employee/me/privacy-notice/accept", json={"version": version}, headers=_bearer(body))
    monkeypatch.setattr(main_module.config, "TASK_RETENTION_DAYS", 5)        # the system's behaviour changed
    assert client.get("/api/employee/me/privacy", headers=_bearer(body)).json()["accepted"] is False


# ── access / export ─────────────────────────────────────────────────────────

def _assign(client, assignee):
    resp = client.post("/api/tasks", json={"task_name": "Sweep", "zone_id": "theatre3", "assigned_minutes": 20,
                                           "assigned_to": assignee})
    assert resp.status_code == 200, resp.text


def test_an_employee_can_download_their_own_data_and_it_contains_no_credentials(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    main_module.employee_directory.set_fcm_token("104", "secret-device-token-abc")
    _assign(client, "104")

    export = client.get("/api/employee/me/data-export", headers=_bearer(body))
    assert export.status_code == 200
    data = export.json()
    assert data["profile"]["name"] == "Sam Rivera" and data["profile"]["phone"] == "+15559000104"
    assert data["profile"]["push_notifications_registered"] is True
    assert data["profile"]["login_password_set"] is True
    assert len(data["tasks"]) == 1 and data["tasks"][0]["task_name"] == "Sweep"
    assert any(e.get("assigned_to") == "104" for e in data["activity_events"])
    raw = export.text
    assert "secret-device-token-abc" not in raw and "scrypt$" not in raw and "password_hash" not in raw


def test_an_export_contains_only_that_employees_data(app_client):
    client, _m, _u = app_client
    mine = _employee(client, "104", "+15559000104")
    _employee(client, "105", "+15559000105")
    _assign(client, "105")
    data = client.get("/api/employee/me/data-export", headers=_bearer(mine)).json()
    assert data["tasks"] == [] and data["activity_events"] == []


def test_data_exports_are_rate_limited(app_client):
    client, _m, _u = app_client
    body = _employee(client)
    codes = [client.get("/api/employee/me/data-export", headers=_bearer(body)).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and 429 in codes


def test_the_export_requires_a_login(app_client):
    client, _m, _u = app_client
    assert client.get("/api/employee/me/data-export", headers={"Authorization": ""}).status_code == 401


def test_an_admin_can_export_any_employees_data_and_a_supervisor_cannot(app_client):
    client, main_module, _u = app_client
    _employee(client)
    admin = _admin(client, main_module)
    assert client.get("/api/admin/employees/104/data-export", headers=admin).json()["profile"]["name"] == "Sam Rivera"
    assert client.get("/api/admin/employees/999/data-export", headers=admin).status_code == 404
    assert client.get("/api/admin/employees/104/data-export").status_code == 403      # the fixture's supervisor


# ── requests register ───────────────────────────────────────────────────────

def test_a_request_gets_a_deadline_and_is_visible_to_the_admin(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    created = client.post("/api/employee/me/privacy-request", json={"kind": "erasure", "details": "Please delete me"},
                          headers=_bearer(body))
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["status"] == "open" and record["overdue"] is False
    due = datetime.fromisoformat(record["due_at"])
    assert timedelta(days=29) < due - datetime.now(timezone.utc) <= timedelta(days=30)

    mine = client.get("/api/employee/me/privacy-requests", headers=_bearer(body)).json()["requests"]
    assert [r["request_id"] for r in mine] == [record["request_id"]]

    admin = _admin(client, main_module)
    listed = client.get("/api/admin/privacy-requests?status=open", headers=admin).json()["requests"]
    assert [r["subject_id"] for r in listed] == ["104"]


def test_an_employee_cannot_see_other_peoples_requests_or_the_admin_list(app_client):
    client, _m, _u = app_client
    a = _employee(client, "104", "+15559000104")
    b = _employee(client, "105", "+15559000105")
    client.post("/api/employee/me/privacy-request", json={"kind": "access"}, headers=_bearer(a))
    assert client.get("/api/employee/me/privacy-requests", headers=_bearer(b)).json()["requests"] == []
    assert client.get("/api/admin/privacy-requests", headers=_bearer(a)).status_code in (401, 403)


def test_a_request_of_an_unknown_kind_is_refused(app_client):
    client, _m, _u = app_client
    body = _employee(client)
    assert client.post("/api/employee/me/privacy-request", json={"kind": "sell-my-data"}, headers=_bearer(body)).status_code == 400


def test_an_overdue_request_is_marked_overdue(app_client, monkeypatch):
    client, main_module, _u = app_client
    body = _employee(client)
    monkeypatch.setattr(main_module.config, "PRIVACY_REQUEST_DUE_DAYS", -1)       # already past due
    client.post("/api/employee/me/privacy-request", json={"kind": "access"}, headers=_bearer(body))
    admin = _admin(client, main_module)
    assert client.get("/api/admin/privacy-requests", headers=admin).json()["requests"][0]["overdue"] is True


def test_closing_a_request_needs_an_outcome_and_records_who_closed_it(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    request_id = client.post("/api/employee/me/privacy-request", json={"kind": "access"},
                             headers=_bearer(body)).json()["request_id"]
    admin = _admin(client, main_module)
    assert client.post(f"/api/admin/privacy-requests/{request_id}/resolve", json={"outcome": "maybe"}, headers=admin).status_code == 400
    ok = client.post(f"/api/admin/privacy-requests/{request_id}/resolve",
                     json={"outcome": "fulfilled", "note": "sent the file"}, headers=admin)
    assert ok.status_code == 200
    closed = client.get("/api/employee/me/privacy-requests", headers=_bearer(body)).json()["requests"][0]
    assert closed["status"] == "resolved" and closed["resolved_by"] == "priv-admin" and closed["outcome"] == "fulfilled"
    # closing twice is refused rather than rewriting history
    assert client.post(f"/api/admin/privacy-requests/{request_id}/resolve", json={"outcome": "fulfilled"}, headers=admin).status_code == 404


# ── erasure ─────────────────────────────────────────────────────────────────

def test_erasure_removes_the_person_but_keeps_the_audit_trail_coherent(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    main_module.employee_directory.set_fcm_token("104", "tok")
    _assign(client, "104")
    admin = _admin(client, main_module)

    resp = client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104"}, headers=admin)
    assert resp.status_code == 200, resp.text

    record = main_module.employee_directory.get("104")
    assert record["name"] == "Former employee" and record["phone"] == "erased:104"
    assert record["fcm_token"] is None and record["password_hash"] is None and record["active"] is False
    assert record["account_username"] is None
    state = main_module.employee_directory.get_privacy_state("104")
    assert state["anonymized_at"] is not None
    # the number remains in task history, but identifies nobody now
    assert any(t["assigned_to"] == "104" for t in main_module.task_store.list_for_employee("104"))
    # and they can no longer sign in — or use the session they had
    assert client.post("/api/employee/auth/login", json={"phone": "+15559000104",
                                                         "password": "correct-horse-battery"}).status_code == 401
    assert client.get("/api/employee/tasks", headers=_bearer(body)).status_code == 401


def test_erasure_demands_the_number_be_repeated(app_client):
    client, main_module, _u = app_client
    _employee(client)
    admin = _admin(client, main_module)
    assert client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "105"}, headers=admin).status_code == 400
    assert main_module.employee_directory.get("104")["name"] == "Sam Rivera"


def test_erasure_is_admin_only_and_cannot_be_repeated_or_reversed(app_client):
    client, main_module, _u = app_client
    _employee(client)
    assert client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104"}).status_code == 403
    admin = _admin(client, main_module)
    assert client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104"}, headers=admin).status_code == 200
    assert client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104"}, headers=admin).status_code == 409
    assert client.post("/api/admin/employees/104/reactivate").status_code == 409        # no resurrecting erased data


def test_erasing_can_close_the_matching_request_in_one_step(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    request_id = client.post("/api/employee/me/privacy-request", json={"kind": "erasure"}, headers=_bearer(body)).json()["request_id"]
    admin = _admin(client, main_module)
    client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104", "request_id": request_id}, headers=admin)
    record = [r for r in client.get("/api/admin/privacy-requests", headers=admin).json()["requests"] if r["request_id"] == request_id][0]
    assert record["status"] == "resolved" and record["outcome"] == "fulfilled"


def test_exports_and_erasures_are_recorded_as_security_events(app_client):
    client, main_module, _u = app_client
    body = _employee(client)
    admin = _admin(client, main_module)
    client.get("/api/employee/me/data-export", headers=_bearer(body))
    client.post("/api/admin/employees/104/erase", json={"confirm_employee_number": "104"}, headers=admin)
    counts = main_module.security_monitor.counts
    assert counts["data_export"] >= 1 and counts["data_erasure"] >= 1


# ── automatic retention ─────────────────────────────────────────────────────

def _backdate_employee_deactivation(main_module, number, days):
    path = main_module.config.EMPLOYEE_DIRECTORY_PATH
    data = json.loads(path.read_text())
    data[number]["deactivated_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(data))


def test_retention_erases_long_deactivated_staff_but_never_active_or_recent_ones(app_client):
    client, main_module, _u = app_client
    for n, ph in (("201", "+15559000201"), ("202", "+15559000202"), ("203", "+15559000203")):
        _add_employee(client, n, "employee", ph, name=f"Person {n}")
    for n in ("201", "202"):
        client.post(f"/api/admin/employees/{n}/deactivate")
    _backdate_employee_deactivation(main_module, "201", main_module.config.EMPLOYEE_RETENTION_DAYS + 5)   # long ago
    # 202 deactivated just now; 203 still active

    summary = main_module.retention_jobs.run_retention(
        config=main_module.config, digest_path=main_module.config.DIGEST_PATH, users=main_module.users,
        event_history=main_module.event_history, task_store=main_module.task_store,
        employee_directory=main_module.employee_directory, privacy_requests=main_module.privacy_requests)

    assert summary["employees"] == {"erased": 1}
    assert main_module.employee_directory.get("201")["name"] == "Former employee"
    assert main_module.employee_directory.get("202")["name"] == "Person 202"
    assert main_module.employee_directory.get("203")["name"] == "Person 203"


def test_a_dry_run_changes_nothing_and_says_what_it_would_do(app_client):
    client, main_module, _u = app_client
    _add_employee(client, "201", "employee", "+15559000201", name="Person 201")
    client.post("/api/admin/employees/201/deactivate")
    _backdate_employee_deactivation(main_module, "201", main_module.config.EMPLOYEE_RETENTION_DAYS + 5)

    summary = main_module.retention_jobs.run_retention(
        config=main_module.config, digest_path=main_module.config.DIGEST_PATH, users=main_module.users,
        event_history=main_module.event_history, task_store=main_module.task_store,
        employee_directory=main_module.employee_directory, privacy_requests=main_module.privacy_requests, dry_run=True)

    assert summary["dry_run"] is True and summary["employees"] == {"would_erase": 1}
    assert main_module.employee_directory.get("201")["name"] == "Person 201"


def test_one_failing_retention_step_does_not_stop_the_others(app_client):
    client, main_module, _u = app_client

    class Broken:
        def purge_older_than(self, *a, **k):
            raise RuntimeError("database hiccup")

    summary = main_module.retention_jobs.run_retention(
        config=main_module.config, digest_path=main_module.config.DIGEST_PATH, users=main_module.users,
        event_history=Broken(), task_store=main_module.task_store,
        employee_directory=main_module.employee_directory, privacy_requests=main_module.privacy_requests)
    assert "error" in summary["event_history"]
    assert "purged" in summary["tasks"] and "erased" in summary["employees"]       # the rest still ran


def test_retention_ends_sessions_of_the_people_it_erases(app_client):
    import asyncio
    client, main_module, _u = app_client
    body = _employee(client)
    client.post("/api/admin/employees/104/deactivate")
    _backdate_employee_deactivation(main_module, "104", main_module.config.EMPLOYEE_RETENTION_DAYS + 5)
    client.portal.call(main_module._retention_once)
    assert main_module.employee_directory.get("104")["name"] == "Former employee"
    assert main_module.security_monitor is not None
    assert main_module.last_retention_run["employees"] == {"erased": 1}


def test_overdue_privacy_requests_raise_an_error_level_alert(app_client, monkeypatch):
    client, main_module, _u = app_client
    body = _employee(client)
    monkeypatch.setattr(main_module.config, "PRIVACY_REQUEST_DUE_DAYS", -1)
    client.post("/api/employee/me/privacy-request", json={"kind": "access"}, headers=_bearer(body))
    alerts = []
    import floorwatch_logging
    floorwatch_logging.set_error_reporter(lambda s, m, f: alerts.append(m))
    try:
        client.portal.call(main_module._retention_once)
    finally:
        floorwatch_logging.set_error_reporter(None)
    assert any("privacy request" in m and "past their deadline" in m for m in alerts)
    assert all("104" not in m for m in alerts)            # nothing identifying goes to the monitoring vendor


def test_resolved_requests_are_kept_for_the_configured_period_then_removed(app_client):
    client, main_module, _u = app_client
    store = main_module.privacy_requests
    old = store.create("104", "access", None, 30)
    store.resolve(old["request_id"], "admin", "fulfilled", None)
    data = json.loads(main_module.config.PRIVACY_REQUESTS_PATH.read_text())
    data[old["request_id"]]["resolved_at"] = (datetime.now(timezone.utc) - timedelta(days=2000)).isoformat()
    main_module.config.PRIVACY_REQUESTS_PATH.write_text(json.dumps(data))
    fresh = store.create("105", "access", None, 30)
    store.resolve(fresh["request_id"], "admin", "fulfilled", None)
    assert store.purge_resolved_older_than(main_module.config.PRIVACY_REQUEST_RETENTION_DAYS) == 1
    assert [r["subject_id"] for r in store.list()] == ["105"]


def test_diagnostics_reports_retention_and_privacy_state(app_client):
    client, main_module, _u = app_client
    admin = _admin(client, main_module)
    body = client.get("/api/admin/diagnostics", headers=admin).json()
    assert body["retention"]["enabled"] is True
    assert body["privacy"]["notice_configured"] is False
    assert body["privacy"]["open_requests"] == 0 and body["privacy"]["overdue_requests"] == 0
