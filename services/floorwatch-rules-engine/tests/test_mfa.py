"""Two-step verification (TOTP): the building blocks, then the whole flow through the
real app for both kinds of login (dashboard accounts and phone staff)."""

import base64
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_auth as auth  # noqa: E402
import floorwatch_crypto as crypto  # noqa: E402
import floorwatch_totp as totp  # noqa: E402

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import app_client, fake_redis_url  # noqa: E402,F401
from test_employee_dashboard_api import _add_employee, _set_password_and_login  # noqa: E402

PASSWORD = "correct-horse-battery-9"


# ── TOTP ────────────────────────────────────────────────────────────────────

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.mark.parametrize("at,expected", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_matches_the_official_rfc_6238_test_vectors(at, expected):
    assert totp.totp_at(RFC_SECRET, at, digits=8) == expected


def test_a_code_is_accepted_within_one_step_either_side_but_not_further():
    now = 1_700_000_000
    code = totp.totp_at(RFC_SECRET, now)
    assert totp.verify_totp(RFC_SECRET, code, now=now) is not None
    assert totp.verify_totp(RFC_SECRET, code, now=now + 30) is not None       # a step late (clock drift)
    assert totp.verify_totp(RFC_SECRET, code, now=now + 120) is None


def test_a_code_cannot_be_used_twice():
    now = 1_700_000_000
    code = totp.totp_at(RFC_SECRET, now)
    step = totp.verify_totp(RFC_SECRET, code, now=now)
    assert totp.verify_totp(RFC_SECRET, code, now=now, last_used_step=step) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5", None])
def test_malformed_codes_are_rejected(bad):
    assert totp.verify_totp(RFC_SECRET, bad, now=1_700_000_000) is None


def test_the_provisioning_uri_is_what_authenticator_apps_expect():
    uri = totp.provisioning_uri("ABCDEFGH", "alice", "Acme Cinemas")
    assert uri.startswith("otpauth://totp/Acme%20Cinemas%3Aalice?secret=ABCDEFGH&issuer=Acme%20Cinemas")


def test_recovery_codes_are_random_and_recognisable():
    codes = totp.generate_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(totp.looks_like_recovery_code(c) for c in codes)
    assert not totp.looks_like_recovery_code("123456")
    assert totp.normalize_recovery_code("ABCD-1234 abcd-1234") == "abcd1234abcd1234"


# ── encryption ──────────────────────────────────────────────────────────────

def test_secrets_round_trip_and_the_ciphertext_hides_them():
    c = crypto.FieldCipher("a-long-dedicated-key-for-tests", "unused")
    stored = c.encrypt("JBSWY3DPEHPK3PXP")
    assert stored.startswith("enc1:") and "JBSWY3DPEHPK3PXP" not in stored
    assert c.decrypt(stored) == "JBSWY3DPEHPK3PXP"


def test_a_database_dump_alone_cannot_be_decrypted_with_the_wrong_key():
    stored = crypto.FieldCipher("key-one-key-one-key-one", "x").encrypt("secret")
    with pytest.raises(ValueError):
        crypto.FieldCipher("a-different-key-entirely", "x").decrypt(stored)
    with pytest.raises(ValueError):
        crypto.FieldCipher("key-one-key-one-key-one", "x").decrypt("plain-not-encrypted")


def test_key_rotation_new_key_encrypts_and_old_data_still_opens():
    old = crypto.FieldCipher("the-old-key-the-old-key-12", "x")
    stored = old.encrypt("secret")
    rotated = crypto.FieldCipher("the-new-key-the-new-key-12,the-old-key-the-old-key-12", "x")
    assert rotated.decrypt(stored) == "secret"                       # old data readable
    fresh = rotated.encrypt("secret2")
    assert crypto.FieldCipher("the-new-key-the-new-key-12", "x").decrypt(fresh) == "secret2"   # new data uses the new key


def test_falling_back_to_the_auth_secret_is_reported():
    assert crypto.FieldCipher("", "some-auth-secret-some-auth-secret").dedicated is False
    assert crypto.FieldCipher("dedicated-key-dedicated", "x").dedicated is True


def test_keyed_hashes_are_stable_per_key_and_different_across_keys():
    a, b = crypto.FieldCipher("key-aaaaaaaaaaaaaaaaaaaa", "x"), crypto.FieldCipher("key-bbbbbbbbbbbbbbbbbbbb", "x")
    assert a.keyed_hash("recovery", "abc") == a.keyed_hash("recovery", "abc")
    assert a.keyed_hash("recovery", "abc") != b.keyed_hash("recovery", "abc")
    assert a.keyed_hash("recovery", "abc") != a.keyed_hash("other-purpose", "abc")


# ── challenge tokens ────────────────────────────────────────────────────────

def test_a_challenge_token_is_not_a_session_token_and_cannot_be_used_as_one():
    secret = "challenge-test-secret-challenge-test"
    challenge = auth.issue_mfa_challenge(secret, "alice", "dashboard", "verify")
    assert auth.verify_token(secret, challenge) is None                       # signed with a different key
    assert auth.verify_mfa_challenge(secret, challenge, "dashboard", "verify") == "alice"
    assert auth.verify_mfa_challenge(secret, challenge, "employee", "verify") is None      # wrong kind
    assert auth.verify_mfa_challenge(secret, challenge, "dashboard", "enroll") is None     # wrong scope
    session = auth.issue_token(secret, "alice", "admin")
    assert auth.verify_mfa_challenge(secret, session, "dashboard", "verify") is None       # and the reverse


def test_an_expired_challenge_is_refused():
    secret = "challenge-test-secret-challenge-test"
    expired = auth.issue_mfa_challenge(secret, "alice", "dashboard", "verify", ttl_seconds=-10)
    assert auth.verify_mfa_challenge(secret, expired, "dashboard", "verify") is None


# ── the whole flow, dashboard accounts ──────────────────────────────────────

def _dash_login(client, username):
    return client.post("/api/login", json={"username": username, "password": PASSWORD}).json()


def _enroll_dashboard(client, main_module, username="dana", role="admin"):
    """Creates the account, signs in, enrols an authenticator. Returns (secret, recovery_codes, headers)."""
    main_module.users.create_user(username, PASSWORD, role=role, must_change_password=False)
    token = _dash_login(client, username)["token"]
    headers = {"Authorization": f"Bearer {token}"}
    secret = client.post("/api/mfa/setup", headers=headers).json()["secret"]
    enabled = client.post("/api/mfa/enable", json={"code": totp.totp_at(secret)}, headers=headers)
    assert enabled.status_code == 200, enabled.text
    return secret, enabled.json()["recovery_codes"], headers


def _next_code(secret):
    """A code for the NEXT time step, so it differs from one already used (codes are single-use)."""
    return totp.totp_at(secret, time.time() + 30)


def test_without_two_step_login_is_unchanged(app_client):
    client, main_module, _u = app_client
    main_module.users.create_user("plain", PASSWORD, role="supervisor", must_change_password=False)
    body = _dash_login(client, "plain")
    assert "token" in body and "mfa_required" not in body


def test_enrolling_returns_ten_recovery_codes_once_and_the_secret_is_encrypted_at_rest(app_client):
    client, main_module, _u = app_client
    secret, codes, headers = _enroll_dashboard(client, main_module)
    assert len(codes) == 10
    assert client.get("/api/mfa/status", headers=headers).json() == {"enabled": True, "recovery_codes_remaining": 10, "required": False}
    raw = main_module.config.MFA_STORE_PATH.read_text()
    assert secret not in raw and "enc1:" in raw
    assert not any(c in raw for c in codes) and not any(c.replace("-", "") in raw for c in codes)


def test_signing_in_with_two_step_needs_the_code_and_yields_no_session_before_it(app_client):
    client, main_module, _u = app_client
    secret, _codes, _h = _enroll_dashboard(client, main_module)
    step_one = _dash_login(client, "dana")
    assert step_one["mfa_required"] is True and "token" not in step_one
    # the challenge can't be used as a session
    assert client.get("/api/state", headers={"Authorization": f"Bearer {step_one['mfa_token']}"}).status_code == 401
    ok = client.post("/api/login/mfa", json={"mfa_token": step_one["mfa_token"], "code": _next_code(secret)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["role"] == "admin" and client.get("/api/state", headers={"Authorization": f"Bearer {ok.json()['token']}"}).status_code == 200


def test_a_wrong_code_is_refused_and_recorded(app_client):
    client, main_module, _u = app_client
    _enroll_dashboard(client, main_module)
    challenge = _dash_login(client, "dana")["mfa_token"]
    before = main_module.security_monitor.counts.get("mfa_failed", 0)
    bad = client.post("/api/login/mfa", json={"mfa_token": challenge, "code": "000000"})
    assert bad.status_code == 401 and "token" not in bad.json()
    assert main_module.security_monitor.counts["mfa_failed"] == before + 1


def test_a_code_works_only_once(app_client):
    client, main_module, _u = app_client
    secret, _c, _h = _enroll_dashboard(client, main_module)
    code = _next_code(secret)
    first = client.post("/api/login/mfa", json={"mfa_token": _dash_login(client, "dana")["mfa_token"], "code": code})
    second = client.post("/api/login/mfa", json={"mfa_token": _dash_login(client, "dana")["mfa_token"], "code": code})
    assert first.status_code == 200 and second.status_code == 401


def test_guessing_codes_is_throttled(app_client):
    client, main_module, _u = app_client
    _enroll_dashboard(client, main_module)
    challenge = _dash_login(client, "dana")["mfa_token"]
    statuses = [client.post("/api/login/mfa", json={"mfa_token": challenge, "code": f"{i:06d}"}).status_code for i in range(8)]
    # 5 attempts per account per 5 minutes in total — and confirming the enrolment code already used one
    assert statuses[:4] == [401] * 4 and 429 in statuses[:6]


def test_a_recovery_code_signs_you_in_once(app_client):
    client, main_module, _u = app_client
    _secret, codes, _h = _enroll_dashboard(client, main_module)
    first = client.post("/api/login/mfa", json={"mfa_token": _dash_login(client, "dana")["mfa_token"], "code": codes[0]})
    assert first.status_code == 200 and first.json()["used_recovery_code"] is True
    again = client.post("/api/login/mfa", json={"mfa_token": _dash_login(client, "dana")["mfa_token"], "code": codes[0]})
    assert again.status_code == 401
    token = first.json()["token"]
    assert client.get("/api/mfa/status", headers={"Authorization": f"Bearer {token}"}).json()["recovery_codes_remaining"] == 9


def test_turning_it_off_needs_the_password_and_a_current_code(app_client):
    client, main_module, _u = app_client
    secret, _c, headers = _enroll_dashboard(client, main_module, role="supervisor")
    assert client.post("/api/mfa/disable", json={"password": "wrong-password-1", "code": _next_code(secret)}, headers=headers).status_code == 401
    assert client.post("/api/mfa/disable", json={"password": PASSWORD, "code": "000000"}, headers=headers).status_code == 401
    assert client.get("/api/mfa/status", headers=headers).json()["enabled"] is True
    ok = client.post("/api/mfa/disable", json={"password": PASSWORD, "code": _next_code(secret)}, headers=headers)
    assert ok.status_code == 200
    assert client.get("/api/mfa/status", headers=headers).json()["enabled"] is False


def test_a_role_that_requires_two_step_cannot_turn_it_off(app_client, monkeypatch):
    client, main_module, _u = app_client
    secret, _c, headers = _enroll_dashboard(client, main_module, role="admin")
    monkeypatch.setattr(main_module.mfa, "required_roles", {"admin"})
    resp = client.post("/api/mfa/disable", json={"password": PASSWORD, "code": _next_code(secret)}, headers=headers)
    assert resp.status_code == 403


def test_a_required_role_cannot_sign_in_without_enrolling_and_is_walked_through_it(app_client, monkeypatch):
    client, main_module, _u = app_client
    monkeypatch.setattr(main_module.mfa, "required_roles", {"admin"})
    main_module.users.create_user("newadmin", PASSWORD, role="admin", must_change_password=False)
    step_one = _dash_login(client, "newadmin")
    assert step_one["mfa_enrollment_required"] is True and "token" not in step_one
    challenge = step_one["mfa_token"]
    # an enrol-scope challenge can't be used to answer a code prompt
    assert client.post("/api/login/mfa", json={"mfa_token": challenge, "code": "123456"}).status_code == 401

    secret = client.post("/api/login/mfa/setup", json={"mfa_token": challenge}).json()["secret"]
    assert client.post("/api/login/mfa/enable", json={"mfa_token": challenge, "code": "000000"}).status_code == 400
    done = client.post("/api/login/mfa/enable", json={"mfa_token": challenge, "code": totp.totp_at(secret)})
    assert done.status_code == 200 and len(done.json()["recovery_codes"]) == 10 and "token" in done.json()


def test_roles_not_required_to_have_it_are_not_forced(app_client, monkeypatch):
    client, main_module, _u = app_client
    monkeypatch.setattr(main_module.mfa, "required_roles", {"admin"})
    main_module.users.create_user("viewer1", PASSWORD, role="viewer", must_change_password=False)
    assert "token" in _dash_login(client, "viewer1")


def test_an_admin_can_reset_a_lost_authenticator_and_that_signs_the_person_out(app_client):
    client, main_module, _u = app_client
    _secret, _c, victim_headers = _enroll_dashboard(client, main_module, "lostphone", role="supervisor")
    main_module.users.create_user("boss", PASSWORD, role="admin", must_change_password=False)
    boss = {"Authorization": "Bearer " + _dash_login(client, "boss")["token"]}
    time.sleep(1.1)
    assert client.post("/api/admin/users/lostphone/mfa/reset", headers=boss).status_code == 200
    assert client.get("/api/state", headers=victim_headers).status_code == 401          # old session ended
    assert "token" in _dash_login(client, "lostphone")                                  # and no code asked any more
    assert client.post("/api/admin/users/lostphone/mfa/reset", headers=boss).status_code == 404
    assert client.post("/api/admin/users/lostphone/mfa/reset", headers=victim_headers).status_code in (401, 403)


def test_the_admin_user_list_shows_who_has_two_step_on(app_client):
    client, main_module, _u = app_client
    _enroll_dashboard(client, main_module, "has2fa", role="admin")
    main_module.users.create_user("no2fa", PASSWORD, role="viewer", must_change_password=False)
    admin = {"Authorization": "Bearer " + _dash_login(client, "has2fa")["mfa_token"]}      # not a session — refused
    assert client.get("/api/admin/users", headers=admin).status_code == 401
    secret_login = _dash_login(client, "has2fa")
    # sign in properly to list
    main_module.config  # noqa: B018
    # use the enrolled account's recovery path: enrol a fresh admin instead (no 2-step) to read the list
    main_module.users.create_user("lister", PASSWORD, role="admin", must_change_password=False)
    lister = {"Authorization": "Bearer " + _dash_login(client, "lister")["token"]}
    flags = {u["username"]: u["mfa_enabled"] for u in client.get("/api/admin/users", headers=lister).json()["users"]}
    assert flags["has2fa"] is True and flags["no2fa"] is False and flags["lister"] is False


# ── phone staff ─────────────────────────────────────────────────────────────

def _employee_login(client, phone):
    return client.post("/api/employee/auth/login", json={"phone": phone, "password": "correct-horse-battery"}).json()


def _enroll_employee(client, number="301", phone="+15559000301", role="employee"):
    _add_employee(client, number, role, phone)
    body = _set_password_and_login(client, number, phone)
    headers = {"Authorization": f"Bearer {body['token']}"}
    secret = client.post("/api/employee/mfa/setup", headers=headers).json()["secret"]
    enabled = client.post("/api/employee/mfa/enable", json={"code": totp.totp_at(secret)}, headers=headers)
    assert enabled.status_code == 200, enabled.text
    return secret, enabled.json()["recovery_codes"], headers


def test_phone_staff_can_enrol_and_must_then_enter_a_code(app_client):
    client, _m, _u = app_client
    secret, _codes, _h = _enroll_employee(client)
    step_one = _employee_login(client, "+15559000301")
    assert step_one["mfa_required"] is True and "token" not in step_one
    ok = client.post("/api/employee/auth/login/mfa", json={"mfa_token": step_one["mfa_token"], "code": _next_code(secret)})
    assert ok.status_code == 200 and ok.json()["employee_number"] == "301" and "token" in ok.json()
    assert client.get("/api/employee/tasks", headers={"Authorization": f"Bearer {ok.json()['token']}"}).status_code == 200


def test_phone_staff_wrong_code_and_replay_are_refused(app_client):
    client, _m, _u = app_client
    secret, _codes, _h = _enroll_employee(client)
    challenge = _employee_login(client, "+15559000301")["mfa_token"]
    assert client.post("/api/employee/auth/login/mfa", json={"mfa_token": challenge, "code": "000000"}).status_code == 401
    code = _next_code(secret)
    assert client.post("/api/employee/auth/login/mfa", json={"mfa_token": challenge, "code": code}).status_code == 200
    assert client.post("/api/employee/auth/login/mfa", json={"mfa_token": challenge, "code": code}).status_code == 401


def test_a_dashboard_challenge_does_not_open_a_phone_sign_in_or_the_reverse(app_client):
    client, main_module, _u = app_client
    secret, _c, _h = _enroll_dashboard(client, main_module, "301", role="admin")      # a dashboard user literally named "301"
    emp_secret, _codes, _h2 = _enroll_employee(client, "301", "+15559000301")
    dash_challenge = _dash_login(client, "301")["mfa_token"]
    assert client.post("/api/employee/auth/login/mfa", json={"mfa_token": dash_challenge, "code": _next_code(emp_secret)}).status_code == 401
    emp_challenge = _employee_login(client, "+15559000301")["mfa_token"]
    assert client.post("/api/login/mfa", json={"mfa_token": emp_challenge, "code": _next_code(secret)}).status_code == 401


def test_a_required_phone_role_is_walked_through_enrolment(app_client, monkeypatch):
    client, main_module, _u = app_client
    monkeypatch.setattr(main_module.mfa, "required_roles", {"secondary_admin"})
    _add_employee(client, "310", "secondary_admin", "+15559000310")
    client.post("/api/admin/employees/310/set-password", json={"password": "correct-horse-battery"})
    step_one = _employee_login(client, "+15559000310")
    assert step_one["mfa_enrollment_required"] is True and "token" not in step_one
    secret = client.post("/api/employee/auth/login/mfa/setup", json={"mfa_token": step_one["mfa_token"]}).json()["secret"]
    done = client.post("/api/employee/auth/login/mfa/enable", json={"mfa_token": step_one["mfa_token"], "code": totp.totp_at(secret)})
    assert done.status_code == 200 and len(done.json()["recovery_codes"]) == 10 and done.json()["role"] == "secondary_admin"


def test_a_deactivated_person_cannot_finish_a_sign_in_already_in_progress(app_client):
    client, _m, _u = app_client
    secret, _codes, _h = _enroll_employee(client)
    challenge = _employee_login(client, "+15559000301")["mfa_token"]
    client.post("/api/admin/employees/301/deactivate")
    assert client.post("/api/employee/auth/login/mfa", json={"mfa_token": challenge, "code": _next_code(secret)}).status_code == 401


def test_phone_staff_can_turn_it_off_with_password_and_code(app_client):
    client, _m, _u = app_client
    secret, _codes, headers = _enroll_employee(client)
    assert client.post("/api/employee/mfa/disable", json={"password": "nope-nope-nope-1", "code": _next_code(secret)}, headers=headers).status_code == 401
    ok = client.post("/api/employee/mfa/disable", json={"password": "correct-horse-battery", "code": _next_code(secret)}, headers=headers)
    assert ok.status_code == 200
    assert client.get("/api/employee/mfa/status", headers=headers).json()["enabled"] is False


def test_an_admin_can_reset_a_phone_staff_authenticator(app_client):
    client, main_module, _u = app_client
    _secret, _c, headers = _enroll_employee(client)
    main_module.users.create_user("resetter", PASSWORD, role="admin", must_change_password=False)
    boss = {"Authorization": "Bearer " + _dash_login(client, "resetter")["token"]}
    time.sleep(1.1)
    assert client.post("/api/admin/employees/301/mfa/reset", headers=boss).status_code == 200
    assert client.get("/api/employee/tasks", headers=headers).status_code == 401
    assert "token" in _employee_login(client, "+15559000301")
    assert client.post("/api/admin/employees/301/mfa/reset", headers=boss).status_code == 404


def test_erasing_a_person_also_deletes_their_authenticator(app_client):
    client, main_module, _u = app_client
    _enroll_employee(client)
    main_module.users.create_user("eraser", PASSWORD, role="admin", must_change_password=False)
    boss = {"Authorization": "Bearer " + _dash_login(client, "eraser")["token"]}
    assert "employee:301" in main_module.mfa.store.enabled_subjects()
    client.post("/api/admin/employees/301/erase", json={"confirm_employee_number": "301"}, headers=boss)
    assert "employee:301" not in main_module.mfa.store.enabled_subjects()


def test_diagnostics_reports_the_two_step_setup(app_client):
    client, main_module, _u = app_client
    main_module.users.create_user("diag", PASSWORD, role="admin", must_change_password=False)
    admin = {"Authorization": "Bearer " + _dash_login(client, "diag")["token"]}
    info = client.get("/api/admin/diagnostics", headers=admin).json()["two_step_verification"]
    assert info["enrolled_accounts"] == 0 and info["required_roles"] == []
    assert "FLOORWATCH_DATA_ENCRYPTION_KEY" in info["encryption_key"]        # the tests run without a dedicated key
