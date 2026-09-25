"""Session revocation for phone-login (employee) accounts.

Dashboard-account revocation already existed (test_admin_users.py). These
cover the gaps on the employee side: server-side logout, force-logout,
revoking on deactivation / a replaced password, and a push token belonging
to one employee at a time.

Revocation is a per-person cutoff on token issue time (whole seconds), so
tests that log in again AFTER a revoke wait past the second boundary, the
way a real person retyping their password would."""

import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401
from test_employee_dashboard_api import _add_employee, _set_password_and_login  # noqa: E402


def _headers(login_body):
    return {"Authorization": f"Bearer {login_body['token']}"}


def _login(client, phone, password="correct-horse-battery"):
    resp = client.post("/api/employee/auth/login", json={"phone": phone, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_logout_kills_the_token_and_clears_the_push_token(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")
    client.post("/api/employee/device-token", json={"fcm_token": "phone-A"}, headers=_headers(body))
    assert main_module.employee_directory.get("101")["fcm_token"] == "phone-A"
    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 200

    assert client.post("/api/employee/auth/logout", headers=_headers(body)).status_code == 200

    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 401
    assert main_module.employee_directory.get("101")["fcm_token"] is None


def test_can_log_in_again_after_logout(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")
    client.post("/api/employee/auth/logout", headers=_headers(body))

    time.sleep(1.1)  # a fresh token must be issued after the revoke's whole second
    again = _login(client, "+15559000101")
    assert client.get("/api/employee/tasks", headers=_headers(again)).status_code == 200


def test_logout_only_affects_that_employee(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    _add_employee(client, "102", "employee", "+15559000102")
    a = _set_password_and_login(client, "101", "+15559000101")
    b = _set_password_and_login(client, "102", "+15559000102")

    client.post("/api/employee/auth/logout", headers=_headers(a))

    assert client.get("/api/employee/tasks", headers=_headers(b)).status_code == 200


def test_an_employee_and_a_dashboard_account_with_the_same_name_dont_revoke_each_other(app_client):
    """Employee numbers and dashboard usernames are separate namespaces —
    both can legally be "104"."""
    client, main_module, _url = app_client
    _add_employee(client, "104", "employee", "+15559000104")
    emp = _set_password_and_login(client, "104", "+15559000104")
    main_module.users.create_user("104", "correct-horse-battery-9", role="supervisor")
    dash = client.post("/api/login", json={"username": "104", "password": "correct-horse-battery-9"})
    assert dash.status_code == 200, dash.text
    dash_headers = {"Authorization": f"Bearer {dash.json()['token']}"}

    client.post("/api/employee/auth/logout", headers=_headers(emp))

    assert client.get("/api/employee/tasks", headers=_headers(emp)).status_code == 401
    assert client.get("/api/state", headers=dash_headers).status_code == 200


def test_deactivating_an_employee_revokes_so_reactivating_does_not_resurrect_the_token(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")

    assert client.post("/api/admin/employees/101/deactivate").status_code == 200
    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 401

    assert client.post("/api/admin/employees/101/reactivate").status_code == 200
    # Before this fix the old token worked again the moment they were reactivated.
    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 401


def test_replacing_an_existing_password_kills_old_sessions(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")

    resp = client.post("/api/admin/employees/101/set-password", json={"password": "another-strong-pass-42"})
    assert resp.status_code == 200, resp.text

    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 401
    time.sleep(1.1)
    fresh = _login(client, "+15559000101", password="another-strong-pass-42")
    assert client.get("/api/employee/tasks", headers=_headers(fresh)).status_code == 200


def test_setting_a_first_password_does_not_revoke(app_client):
    """Onboarding: no earlier sessions exist, and revoking would reject a
    login made in the same second as the set."""
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")  # set + immediate login
    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 200


def test_admin_can_force_logout_without_deactivating(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    body = _set_password_and_login(client, "101", "+15559000101")

    assert client.post("/api/admin/employees/101/logout").status_code == 200

    assert client.get("/api/employee/tasks", headers=_headers(body)).status_code == 401
    assert main_module.employee_directory.get("101")["active"] is True  # still an active employee
    assert client.post("/api/admin/employees/999/logout").status_code == 404


def test_phone_admins_can_force_logout_but_a_plain_supervisor_cannot(app_client):
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    _add_employee(client, "300", "secondary_admin", "+15559000300")
    _add_employee(client, "301", "supervisor", "+15559000301")
    target = _set_password_and_login(client, "101", "+15559000101")
    manager = _set_password_and_login(client, "300", "+15559000300")
    supervisor = _set_password_and_login(client, "301", "+15559000301")

    resp = client.post("/api/employee/dashboard/employees/101/logout", headers=_headers(supervisor))
    assert resp.status_code == 403
    assert client.get("/api/employee/tasks", headers=_headers(target)).status_code == 200

    resp = client.post("/api/employee/dashboard/employees/101/logout", headers=_headers(manager))
    assert resp.status_code == 200, resp.text
    assert client.get("/api/employee/tasks", headers=_headers(target)).status_code == 401


def test_a_push_token_belongs_to_one_employee_at_a_time(app_client):
    """Two people sharing a phone: the second login registers the same
    install's token. The first person's record must stop pointing at it,
    or their task notifications keep arriving on the second person's screen."""
    client, main_module, _url = app_client
    _add_employee(client, "101", "employee", "+15559000101")
    _add_employee(client, "102", "employee", "+15559000102")
    a = _set_password_and_login(client, "101", "+15559000101")
    b = _set_password_and_login(client, "102", "+15559000102")

    client.post("/api/employee/device-token", json={"fcm_token": "shared-phone"}, headers=_headers(a))
    client.post("/api/employee/device-token", json={"fcm_token": "shared-phone"}, headers=_headers(b))

    assert main_module.employee_directory.get("101")["fcm_token"] is None
    assert main_module.employee_directory.get("102")["fcm_token"] == "shared-phone"


def test_dashboard_account_logout_kills_its_token(app_client):
    client, main_module, _url = app_client
    main_module.users.create_user("ops.lead", "correct-horse-battery-9", role="supervisor")
    login = client.post("/api/login", json={"username": "ops.lead", "password": "correct-horse-battery-9"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/state", headers=headers).status_code == 200

    assert client.post("/api/logout", headers=headers).status_code == 200

    assert client.get("/api/state", headers=headers).status_code == 401
