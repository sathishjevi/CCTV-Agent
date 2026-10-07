"""API hardening, end to end against the real app: strict request bodies,
no input echo in errors, login rate limiting that can't be dodged with a
header, security events + alerting, and tighter CORS."""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401
from test_employee_dashboard_api import _add_employee, _set_password_and_login  # noqa: E402

GOOD_TASK = {"task_name": "Sweep", "zone_id": "theatre3", "assigned_minutes": 15}


# ── strict bodies ───────────────────────────────────────────────────────────

def test_unknown_fields_are_rejected_not_silently_dropped(app_client):
    client, _m, _u = app_client
    resp = client.post("/api/tasks", json={**GOOD_TASK, "is_admin": True})
    assert resp.status_code == 422
    assert "is_admin" in resp.json()["error"]


def test_a_known_good_body_still_works(app_client):
    client, _m, _u = app_client
    assert client.post("/api/tasks", json=GOOD_TASK).status_code == 200


@pytest.mark.parametrize("minutes", [0, -5, 1441, 10 ** 12, float("inf"), float("nan")])
def test_task_budgets_must_be_a_sane_positive_number(app_client, minutes):
    """NaN/inf serialise to invalid JSON in the shared snapshot, which would
    break every dashboard and app for every user; 0 divides by zero in them."""
    client, _m, _u = app_client
    import json
    resp = client.post("/api/tasks", content=json.dumps({**GOOD_TASK, "assigned_minutes": minutes}),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 422, resp.text


def test_text_fields_have_a_ceiling(app_client):
    client, _m, _u = app_client
    assert client.post("/api/tasks", json={**GOOD_TASK, "task_name": "x" * 5000}).status_code == 422
    assert client.post("/api/tasks", json={**GOOD_TASK, "task_name": "x" * 120}).status_code == 200


def test_a_huge_password_is_rejected_before_it_is_ever_hashed(app_client):
    client, _m, _u = app_client
    resp = client.post("/api/login", json={"username": "test-supervisor", "password": "p" * 100_000})
    assert resp.status_code == 422


def test_validation_errors_never_echo_the_submitted_value(app_client):
    client, _m, _u = app_client
    secret = "SuperSecretPassword" * 30     # > 256 chars so it is rejected
    resp = client.post("/api/login", json={"username": "test-supervisor", "password": secret})
    assert resp.status_code == 422
    assert "SuperSecretPassword" not in resp.text
    body = resp.json()
    assert isinstance(body["error"], str) and "password" in body["error"]
    assert all("input" not in d for d in body["detail"])


def test_the_phone_login_body_is_strict_too(app_client):
    client, _m, _u = app_client
    resp = client.post("/api/employee/auth/login", json={"phone": "+15550000000", "password": "x", "extra": 1})
    assert resp.status_code == 422


# ── login throttling that a header cannot dodge ─────────────────────────────

def test_rotating_a_fake_forwarded_for_does_not_bypass_the_per_ip_login_limit(app_client):
    """Before: the leftmost X-Forwarded-For entry was trusted, so a fresh fake
    value per attempt gave every attempt its own budget."""
    client, _m, _u = app_client
    statuses = []
    for i in range(14):
        resp = client.post("/api/login", json={"username": f"guess{i}", "password": "wrong-password-1"},
                           headers={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.50", "Authorization": ""})
        statuses.append(resp.status_code)
    assert 429 in statuses, statuses
    assert statuses.index(429) <= 10


def test_one_venue_ip_can_log_many_staff_in_within_a_minute(app_client):
    """A shift change: more than the old limit of 10 phones signing in from the
    one public IP the venue's wifi presents."""
    client, main_module, _u = app_client
    for i in range(15):
        _add_employee(client, f"7{i:02d}", "employee", f"+155590007{i:02d}")
    for i in range(15):
        body = _set_password_and_login(client, f"7{i:02d}", f"+155590007{i:02d}")
        assert "token" in body


def test_phone_login_per_phone_limit_still_stops_guessing_one_account(app_client):
    client, _m, _u = app_client
    _add_employee(client, "800", "employee", "+15559000800")
    codes = [client.post("/api/employee/auth/login",
                         json={"phone": "+15559000800", "password": f"wrong-pass-{i}"}).status_code
             for i in range(9)]
    assert codes[:5] == [401] * 5 and 429 in codes


# ── security events ─────────────────────────────────────────────────────────

def test_failed_and_successful_logins_are_recorded(app_client):
    client, main_module, _u = app_client
    before = dict(main_module.security_monitor.counts)
    client.post("/api/login", json={"username": "nobody", "password": "whatever-123"})
    client.post("/api/login", json={"username": "test-supervisor", "password": "test-password-123"})
    after = main_module.security_monitor.counts
    assert after["login_failed"] == before.get("login_failed", 0) + 1
    assert after["login_succeeded"] == before.get("login_succeeded", 0) + 1


def test_passwords_never_appear_in_security_logs(app_client, capsys):
    client, _m, _u = app_client
    client.post("/api/login", json={"username": "test-supervisor", "password": "Zx9-unique-wrong-password"})
    assert "Zx9-unique-wrong-password" not in capsys.readouterr().err


def test_a_burst_of_failed_logins_raises_one_alert(app_client):
    client, main_module, _u = app_client
    main_module.login_rate_limiter_by_ip.max_per_window = 1000          # let the burst reach the monitor
    main_module.login_rate_limiter_by_username.max_per_window = 1000
    for i in range(12):
        client.post("/api/login", json={"username": "test-supervisor", "password": f"wrong-password-{i}"})
    assert main_module.security_monitor.counts["alerts_raised"] >= 1


def test_authorization_failures_are_recorded(app_client):
    client, main_module, _u = app_client       # a supervisor, reaching for an admin-only route
    before = main_module.security_monitor.counts.get("authz_denied", 0)
    assert client.get("/api/admin/users").status_code == 403
    assert main_module.security_monitor.counts["authz_denied"] == before + 1


def test_scanner_probes_of_unknown_routes_are_recorded(app_client):
    client, main_module, _u = app_client
    before = main_module.security_monitor.counts.get("not_found", 0)
    assert client.get("/.env").status_code == 404
    assert client.get("/wp-login.php").status_code == 404
    assert main_module.security_monitor.counts["not_found"] == before + 2


# ── CORS ────────────────────────────────────────────────────────────────────

def test_cors_preflight_only_allows_the_methods_and_headers_in_use(app_client):
    client, main_module, _u = app_client
    origin = main_module.config.CORS_ALLOWED_ORIGINS[0]
    ok = client.options("/api/state", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert ok.status_code == 200
    bad_method = client.options("/api/state", headers={"Origin": origin, "Access-Control-Request-Method": "PATCH"})
    assert bad_method.status_code == 400
    bad_header = client.options("/api/state", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-evil"})
    assert bad_header.status_code == 400


# ── diagnostics ─────────────────────────────────────────────────────────────

def test_diagnostics_reports_database_state_and_security_counters(app_client):
    client, main_module, _u = app_client
    main_module.users.create_user("diag-admin-2", "correct-horse-battery-9", role="admin", must_change_password=False)
    token = client.post("/api/login", json={"username": "diag-admin-2", "password": "correct-horse-battery-9"}).json()["token"]
    body = client.get("/api/admin/diagnostics", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["database_connection"] == {"configured": False}      # tests run on the local-file fallback
    assert "login_succeeded" in body["security_events"]
    assert set(body["storage_backend"]) == {"employees", "zones", "history", "tasks"}
