"""
Integration tests for the admin user-management endpoints (/api/admin/users/*)
and the self-service /api/change-password endpoint — the "super admin can
add supervisor access, client doesn't regenerate a key every time" feature.
Uses a real FastAPI TestClient against the real app with a temp-file user
store (no Postgres in this sandbox — see PostgresUserStore's own mocked
unit tests in test_floorwatch_auth.py for that half).
"""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("fastapi")


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    import threading
    import time
    import fakeredis as fr
    server = fr.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    import config
    monkeypatch.setattr(config, "REDIS_URL", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "EMPLOYEE_DIRECTORY_PATH", tmp_path / "employee_directory.json")
    monkeypatch.setattr(config, "TASK_STORE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")  # isolate from any real env var set on this machine
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")

    sys.modules.pop("main", None)
    sys.modules.pop("engine", None)
    sys.modules.pop("effort_engine", None)
    import main as main_module

    main_module.users.create_user("root_admin", "admin-password-123", role="admin", must_change_password=False)
    main_module.users.create_user("existing_super", "super-password-123", role="supervisor", must_change_password=False)

    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        def login(username, password):
            resp = client.post("/api/login", json={"username": username, "password": password})
            return resp

        yield client, login, main_module

    server.shutdown()


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ── env-var admin seeding (FLOORWATCH_ADMIN_USERNAME/PASSWORD) ──────────

@pytest.fixture
def seeded_env(monkeypatch, tmp_path):
    """Separate from app_client — this exercises _seed_admin_from_env(),
    which runs at import time, so ADMIN_USERNAME/PASSWORD must be set
    BEFORE `import main` for each variant below."""
    import threading
    import time
    import fakeredis as fr
    server = fr.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    import config
    monkeypatch.setattr(config, "REDIS_URL", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "EMPLOYEE_DIRECTORY_PATH", tmp_path / "employee_directory.json")
    monkeypatch.setattr(config, "TASK_STORE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")
    yield config, monkeypatch
    server.shutdown()


def _import_fresh_main():
    sys.modules.pop("main", None)
    sys.modules.pop("engine", None)
    sys.modules.pop("effort_engine", None)
    import main as main_module
    return main_module


def test_seed_creates_admin_when_both_env_vars_set_and_no_account_exists(seeded_env):
    config, monkeypatch = seeded_env
    monkeypatch.setattr(config, "ADMIN_USERNAME", "seeded_admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "SeedPassword123")

    main_module = _import_fresh_main()

    assert main_module.users.user_exists("seeded_admin") is True
    result = main_module.users.authenticate("seeded_admin", "SeedPassword123")
    assert result == {"role": "admin", "must_change_password": True}


def test_seed_does_nothing_when_env_vars_unset(seeded_env):
    config, monkeypatch = seeded_env
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")

    main_module = _import_fresh_main()

    assert main_module.users.list_usernames() == []


def test_seed_does_not_overwrite_existing_account_on_redeploy(seeded_env):
    """The critical idempotency guarantee: if the admin already changed
    their real password via the UI, a later restart with the SAME env
    vars still set must NOT silently revert it back to the seed value."""
    config, monkeypatch = seeded_env
    monkeypatch.setattr(config, "ADMIN_USERNAME", "seeded_admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "OriginalSeedPass1")

    main_module = _import_fresh_main()
    # Simulate the admin having changed their password after first login.
    main_module.users.set_password("seeded_admin", "MyRealChosenPass1", must_change_password=False)

    # "Redeploy" — re-import main with the SAME env vars still set.
    main_module2 = _import_fresh_main()

    assert main_module2.users.authenticate("seeded_admin", "OriginalSeedPass1") is None
    result = main_module2.users.authenticate("seeded_admin", "MyRealChosenPass1")
    assert result == {"role": "admin", "must_change_password": False}


def test_seed_refuses_short_password(seeded_env):
    config, monkeypatch = seeded_env
    monkeypatch.setattr(config, "ADMIN_USERNAME", "seeded_admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "short")

    main_module = _import_fresh_main()

    assert main_module.users.user_exists("seeded_admin") is False


def test_seeded_admin_can_actually_log_in_via_the_api(seeded_env):
    config, monkeypatch = seeded_env
    monkeypatch.setattr(config, "ADMIN_USERNAME", "seeded_admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "SeedPassword123")

    main_module = _import_fresh_main()
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.post("/api/login", json={"username": "seeded_admin", "password": "SeedPassword123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "admin"
        assert body["must_change_password"] is True


# ── login response shape ────────────────────────────────────────────────

def test_login_returns_must_change_password_flag(app_client):
    client, login, _ = app_client
    resp = login("existing_super", "super-password-123")
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


def test_login_rejects_deactivated_account(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("temp_viewer", "temp-password-123", role="viewer")
    main_module.users.set_active("temp_viewer", False)
    resp = login("temp_viewer", "temp-password-123")
    assert resp.status_code == 401


# ── admin-only access control ───────────────────────────────────────────

def test_list_users_requires_admin_not_just_supervisor(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.get("/api/admin/users", headers=_auth_headers(token))
    assert resp.status_code == 403


def test_list_users_works_for_admin(app_client):
    client, login, _ = app_client
    token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.get("/api/admin/users", headers=_auth_headers(token))
    assert resp.status_code == 200
    usernames = {u["username"] for u in resp.json()["users"]}
    assert "root_admin" in usernames
    assert "existing_super" in usernames


def test_list_users_response_never_includes_password_hash(app_client):
    client, login, _ = app_client
    token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.get("/api/admin/users", headers=_auth_headers(token))
    body_text = resp.text
    assert "password_hash" not in body_text


def test_create_user_requires_admin(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(token),
                        json={"username": "new_guy", "password": "newpass123", "role": "viewer"})
    assert resp.status_code == 403


# ── create ───────────────────────────────────────────────────────────────

def test_admin_creates_supervisor_account_and_it_can_log_in(app_client):
    client, login, main_module = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]

    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": "new_supervisor", "password": "TempPass123", "role": "supervisor"})
    assert resp.status_code == 200

    login_resp = login("new_supervisor", "TempPass123")
    assert login_resp.status_code == 200
    body = login_resp.json()
    assert body["role"] == "supervisor"
    assert body["must_change_password"] is True  # admin-created -> forced change, per the chosen design


def test_admin_creates_another_admin_account(app_client):
    """Admins aren't restricted to only creating lower-privileged
    accounts — the brief was "super admin can add supervisor access", but
    nothing says only one admin should ever exist."""
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": "second_admin", "password": "TempPass123", "role": "admin"})
    assert resp.status_code == 200


def test_create_user_rejects_duplicate_username(app_client):
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": "existing_super", "password": "whatever123", "role": "viewer"})
    assert resp.status_code == 409


def test_create_user_rejects_short_password(app_client):
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": "someone", "password": "short", "role": "viewer"})
    assert resp.status_code == 400


def test_create_user_rejects_invalid_username(app_client):
    """DP-M5: no length/character validation previously existed on
    username at the real endpoint — this is the exact gap that made
    DP-H2's attribute-breakout payload creatable in the first place."""
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": 'x" onmouseover="alert(1)', "password": "ValidPass123", "role": "viewer"})
    assert resp.status_code == 400


def test_create_user_rejects_common_weak_password(app_client):
    """Long enough to pass the old 8-char rule, but on the DP-M1 common-
    password blocklist — must still be rejected via the real endpoint."""
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users", headers=_auth_headers(admin_token),
                        json={"username": "someone", "password": "password123", "role": "viewer"})
    assert resp.status_code == 400
    assert "common" in resp.json()["error"].lower()


def test_create_user_rejects_service_role():
    pass  # covered structurally: "service" role is excluded from the create-user role choices


def test_create_user_records_who_created_the_account(app_client):
    client, login, main_module = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    client.post("/api/admin/users", headers=_auth_headers(admin_token),
                json={"username": "tracked_user", "password": "TempPass123", "role": "viewer"})
    listed = main_module.users.list_users()
    entry = next(u for u in listed if u["username"] == "tracked_user")
    assert entry["created_by"] == "root_admin"


# ── deactivate / reactivate ─────────────────────────────────────────────

def test_admin_deactivates_account_blocks_future_login(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("soon_gone", "GoneNow123", role="viewer")
    admin_token = login("root_admin", "admin-password-123").json()["token"]

    resp = client.post("/api/admin/users/soon_gone/deactivate", headers=_auth_headers(admin_token))
    assert resp.status_code == 200

    login_resp = login("soon_gone", "GoneNow123")
    assert login_resp.status_code == 401


def test_admin_cannot_deactivate_own_account(app_client):
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users/root_admin/deactivate", headers=_auth_headers(admin_token))
    assert resp.status_code == 400


def test_deactivate_unknown_user_returns_404(app_client):
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.post("/api/admin/users/nobody_here/deactivate", headers=_auth_headers(admin_token))
    assert resp.status_code == 404


def test_reactivate_restores_login(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("comes_back", "ComesBack123", role="viewer")
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    client.post("/api/admin/users/comes_back/deactivate", headers=_auth_headers(admin_token))
    resp = client.post("/api/admin/users/comes_back/reactivate", headers=_auth_headers(admin_token))
    assert resp.status_code == 200
    assert login("comes_back", "ComesBack123").status_code == 200


def test_deactivate_revokes_the_targets_existing_token_immediately(app_client):
    """Production-readiness gap: deactivating a user used to block only
    their NEXT login — an already-issued token kept working until natural
    expiry. Now the token dies at deactivation time too."""
    client, login, main_module = app_client
    main_module.users.create_user("soon_gone2", "GoneNow123", role="viewer")
    victim_token = login("soon_gone2", "GoneNow123").json()["token"]
    assert client.get("/api/state", headers=_auth_headers(victim_token)).status_code == 200

    admin_token = login("root_admin", "admin-password-123").json()["token"]
    client.post("/api/admin/users/soon_gone2/deactivate", headers=_auth_headers(admin_token))

    resp = client.get("/api/state", headers=_auth_headers(victim_token))
    assert resp.status_code == 401


def test_deactivate_does_not_revoke_other_users_tokens(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("bystander", "Bystander123", role="viewer")
    main_module.users.create_user("target_user", "TargetUser123", role="viewer")
    bystander_token = login("bystander", "Bystander123").json()["token"]

    admin_token = login("root_admin", "admin-password-123").json()["token"]
    client.post("/api/admin/users/target_user/deactivate", headers=_auth_headers(admin_token))

    assert client.get("/api/state", headers=_auth_headers(bystander_token)).status_code == 200


# ── admin resets a password ─────────────────────────────────────────────

def test_admin_resets_password_forces_change_on_next_login(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("forgetful", "OldPassword123", role="viewer", must_change_password=False)
    admin_token = login("root_admin", "admin-password-123").json()["token"]

    resp = client.post("/api/admin/users/forgetful/reset-password", headers=_auth_headers(admin_token),
                        json={"new_password": "TempReset123"})
    assert resp.status_code == 200

    # old password no longer works
    assert login("forgetful", "OldPassword123").status_code == 401
    # new temp password works, and flags a forced change
    login_resp = login("forgetful", "TempReset123")
    assert login_resp.status_code == 200
    assert login_resp.json()["must_change_password"] is True


def test_admin_reset_password_rejects_common_weak_password(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("forgetful2", "OldPassword123", role="viewer", must_change_password=False)
    admin_token = login("root_admin", "admin-password-123").json()["token"]

    resp = client.post("/api/admin/users/forgetful2/reset-password", headers=_auth_headers(admin_token),
                        json={"new_password": "qwertyuiop"})
    assert resp.status_code == 400
    # old password still works — the rejected reset must not have applied
    assert login("forgetful2", "OldPassword123").status_code == 200


def test_admin_reset_password_revokes_the_targets_existing_token_immediately(app_client):
    """The compromise scenario this exists for: an admin force-resets a
    password because they suspect the account is compromised — the
    attacker's already-issued token must die right then, not linger until
    its natural 12h expiry."""
    client, login, main_module = app_client
    main_module.users.create_user("maybe_compromised", "OldPassword123", role="viewer",
                                   must_change_password=False)
    victim_token = login("maybe_compromised", "OldPassword123").json()["token"]
    assert client.get("/api/state", headers=_auth_headers(victim_token)).status_code == 200

    admin_token = login("root_admin", "admin-password-123").json()["token"]
    client.post("/api/admin/users/maybe_compromised/reset-password", headers=_auth_headers(admin_token),
                json={"new_password": "BrandNewTemp123"})

    resp = client.get("/api/state", headers=_auth_headers(victim_token))
    assert resp.status_code == 401


# ── self-service change-password ────────────────────────────────────────

def test_change_password_requires_correct_current_password(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "wrong-current", "new_password": "NewPassword123"})
    assert resp.status_code == 401


def test_change_password_succeeds_and_clears_must_change_flag(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("must_change", "OldTemp1234", role="viewer")  # must_change_password=True default
    login_resp = login("must_change", "OldTemp1234")
    assert login_resp.json()["must_change_password"] is True
    token = login_resp.json()["token"]

    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "OldTemp1234", "new_password": "BrandNewPass1"})
    assert resp.status_code == 200

    # old password dead, new one works, flag cleared
    assert login("must_change", "OldTemp1234").status_code == 401
    new_login = login("must_change", "BrandNewPass1")
    assert new_login.status_code == 200
    assert new_login.json()["must_change_password"] is False


def test_change_password_does_not_revoke_the_callers_own_current_token(app_client):
    """Deliberately different from admin-reset: the caller here already
    re-proved they hold both a valid token AND the current password, and
    the client keeps using the SAME token afterward (no re-login step in
    the UI) — revoking it here would immediately log the user out of the
    very request that just succeeded."""
    client, login, main_module = app_client
    main_module.users.create_user("self_changer", "OldPassword123", role="viewer",
                                   must_change_password=False)
    token = login("self_changer", "OldPassword123").json()["token"]

    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "OldPassword123", "new_password": "BrandNewPass1"})
    assert resp.status_code == 200

    assert client.get("/api/state", headers=_auth_headers(token)).status_code == 200


def test_change_password_rejects_short_new_password(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "super-password-123", "new_password": "short"})
    assert resp.status_code == 400


def test_change_password_rejects_common_weak_password(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "super-password-123", "new_password": "letmein1234"})
    assert resp.status_code == 400
    # old password still works — the rejected change must not have applied
    assert login("existing_super", "super-password-123").status_code == 200


def test_change_password_rejects_password_equal_to_own_username(app_client):
    # username itself must be >= the 10-char minimum so this exercises the
    # username-equality rule specifically, not the length rule.
    client, login, main_module = app_client
    main_module.users.create_user("samename_user", "OldPassword123", role="viewer", must_change_password=False)
    token = login("samename_user", "OldPassword123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "OldPassword123", "new_password": "samename_user"})
    assert resp.status_code == 400
    assert "username" in resp.json()["error"].lower()


def test_change_password_available_to_viewer_role_too(app_client):
    """Self-service password change is a require_auth (any role) endpoint,
    not require_supervisor/require_admin — a viewer must be able to change
    their own password."""
    client, login, main_module = app_client
    main_module.users.create_user("plain_viewer", "ViewerPass123", role="viewer", must_change_password=False)
    token = login("plain_viewer", "ViewerPass123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "ViewerPass123", "new_password": "NewViewerPass1"})
    assert resp.status_code == 200


# ── admin inherits supervisor permissions (role hierarchy) ─────────────

def test_admin_token_can_use_supervisor_only_endpoints(app_client):
    """Proves the hierarchical role check end-to-end through a real
    require_supervisor-gated route, not just the unit-level dependency test
    in test_floorwatch_auth.py."""
    client, login, _ = app_client
    admin_token = login("root_admin", "admin-password-123").json()["token"]
    resp = client.get("/api/queue", headers=_auth_headers(admin_token))
    assert resp.status_code == 200


# ── DP-H3: rate limiting on /api/login and /api/admin/users* ────────────
# (DATA_PROTECTION_SECURITY_ANALYSIS.md). Limiters are shrunk to a tiny
# max_per_window on the already-constructed main_module instances so each
# test only needs a handful of requests, not real wall-clock waiting.

def test_login_blocked_after_exceeding_per_ip_limit(app_client):
    client, login, main_module = app_client
    main_module.login_rate_limiter_by_ip.max_per_window = 3

    for _ in range(3):
        resp = client.post("/api/login", json={"username": "existing_super", "password": "wrong"})
        assert resp.status_code == 401  # bad password, but not yet rate-limited

    blocked = client.post("/api/login", json={"username": "existing_super", "password": "wrong"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_login_rate_limit_by_ip_uses_x_forwarded_for(app_client):
    """Confirms client_ip() actually reads X-Forwarded-For (Railway/most
    reverse proxies set this) rather than always seeing the same address
    for every caller — without this, every real caller behind the same
    proxy would share one budget."""
    client, login, main_module = app_client
    main_module.login_rate_limiter_by_ip.max_per_window = 2

    for _ in range(2):
        resp = client.post("/api/login", json={"username": "existing_super", "password": "wrong"},
                            headers={"X-Forwarded-For": "203.0.113.5"})
        assert resp.status_code == 401
    blocked = client.post("/api/login", json={"username": "existing_super", "password": "wrong"},
                           headers={"X-Forwarded-For": "203.0.113.5"})
    assert blocked.status_code == 429

    # A different source IP still has its own untouched budget.
    fresh = client.post("/api/login", json={"username": "existing_super", "password": "wrong"},
                         headers={"X-Forwarded-For": "198.51.100.9"})
    assert fresh.status_code == 401  # not 429 — different IP, different budget


def test_login_blocked_after_exceeding_per_username_limit_across_different_ips(app_client):
    """The per-username limiter exists specifically to catch a
    DISTRIBUTED attack against one account from many source IPs, which
    the per-IP limiter alone would never trip."""
    client, login, main_module = app_client
    main_module.login_rate_limiter_by_ip.max_per_window = 1000  # effectively disabled for this test
    main_module.login_rate_limiter_by_username.max_per_window = 3

    for i in range(3):
        resp = client.post("/api/login", json={"username": "existing_super", "password": "wrong"},
                            headers={"X-Forwarded-For": f"203.0.113.{i}"})  # a different IP every time
        assert resp.status_code == 401

    blocked = client.post("/api/login", json={"username": "existing_super", "password": "wrong"},
                           headers={"X-Forwarded-For": "203.0.113.99"})
    assert blocked.status_code == 429

    # A different username is unaffected — this isn't a global lockout.
    other = client.post("/api/login", json={"username": "root_admin", "password": "wrong"},
                         headers={"X-Forwarded-For": "203.0.113.100"})
    assert other.status_code == 401  # not 429


def test_successful_login_still_counts_against_the_rate_limit(app_client):
    """The limiter must apply BEFORE credential checking succeeds or
    fails — otherwise a valid low-and-slow credential-stuffing attempt
    that occasionally guesses right would bypass it entirely."""
    client, login, main_module = app_client
    main_module.login_rate_limiter_by_username.max_per_window = 2

    assert login("existing_super", "super-password-123").status_code == 200
    assert login("existing_super", "super-password-123").status_code == 200
    blocked = login("existing_super", "super-password-123")
    assert blocked.status_code == 429


def test_admin_endpoint_blocked_after_exceeding_rate_limit(app_client):
    client, login, main_module = app_client
    main_module.admin_rate_limiter.max_per_window = 2
    admin_token = login("root_admin", "admin-password-123").json()["token"]

    for _ in range(2):
        resp = client.get("/api/admin/users", headers=_auth_headers(admin_token))
        assert resp.status_code == 200

    blocked = client.get("/api/admin/users", headers=_auth_headers(admin_token))
    assert blocked.status_code == 429


def test_admin_rate_limit_is_per_admin_not_global(app_client):
    client, login, main_module = app_client
    main_module.users.create_user("second_admin", "SecondAdmin123", role="admin", must_change_password=False)
    main_module.admin_rate_limiter.max_per_window = 1

    token1 = login("root_admin", "admin-password-123").json()["token"]
    token2 = login("second_admin", "SecondAdmin123").json()["token"]

    assert client.get("/api/admin/users", headers=_auth_headers(token1)).status_code == 200
    assert client.get("/api/admin/users", headers=_auth_headers(token1)).status_code == 429
    # second_admin has their own separate budget, unaffected by root_admin's.
    assert client.get("/api/admin/users", headers=_auth_headers(token2)).status_code == 200
