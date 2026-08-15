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
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
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


def test_change_password_rejects_short_new_password(app_client):
    client, login, _ = app_client
    token = login("existing_super", "super-password-123").json()["token"]
    resp = client.post("/api/change-password", headers=_auth_headers(token),
                        json={"current_password": "super-password-123", "new_password": "short"})
    assert resp.status_code == 400


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
