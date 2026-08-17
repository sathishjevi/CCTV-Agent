"""Unit tests for the shared skills/lib/floorwatch_auth.py module."""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402

from floorwatch_auth import (  # noqa: E402
    ROLE_RANK, UserStore, PostgresUserStore, RevocationStore, build_user_store, hash_password,
    verify_password, issue_token, verify_token, get_or_create_secret, make_auth_dependency,
    validate_password_strength, validate_username, purge_stale_deactivated_accounts, verify_ws_token,
)

SECRET = "test-secret-do-not-use-in-prod-needs-32-bytes-minimum"


# ── password hashing ──────────────────────────────────────────────────────

def test_hash_and_verify_password_roundtrip():
    stored = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", stored) is True


def test_verify_password_rejects_wrong_password():
    stored = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", stored) is False


def test_hash_password_uses_random_salt_each_time():
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2  # different salts -> different stored strings
    assert verify_password("same-password", h1)
    assert verify_password("same-password", h2)


def test_verify_password_rejects_malformed_stored_value():
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "") is False


# ── password policy (DP-M1, DATA_PROTECTION_SECURITY_ANALYSIS.md) ─────────

def test_validate_password_strength_accepts_reasonable_password():
    ok, reason = validate_password_strength("correct-horse-battery")
    assert ok is True
    assert reason is None


def test_validate_password_strength_rejects_too_short():
    ok, reason = validate_password_strength("Sh0rt!")
    assert ok is False
    assert "10" in reason  # states the minimum length


def test_validate_password_strength_accepts_password_at_exact_minimum_length():
    ok, _ = validate_password_strength("abcdefghij")  # exactly 10 chars
    assert ok is True


def test_validate_password_strength_rejects_common_weak_password():
    ok, reason = validate_password_strength("password123")
    assert ok is False
    assert "common" in reason.lower()


def test_validate_password_strength_common_password_check_is_case_insensitive():
    ok, _ = validate_password_strength("PASSWORD123")
    assert ok is False


def test_validate_password_strength_rejects_password_equal_to_username():
    ok, reason = validate_password_strength("supervisorpat", username="supervisorpat")
    assert ok is False
    assert "username" in reason.lower()


def test_validate_password_strength_username_check_is_case_insensitive():
    ok, _ = validate_password_strength("SupervisorPat", username="supervisorpat")
    assert ok is False


def test_validate_password_strength_allows_password_containing_but_not_equal_to_username():
    # only an exact match is rejected — a password merely containing the
    # username isn't a policy violation on its own.
    ok, _ = validate_password_strength("alice-loves-cameras", username="alice")
    assert ok is True


# ── username policy (DP-M5, DATA_PROTECTION_SECURITY_ANALYSIS.md) ─────────

def test_validate_username_accepts_reasonable_username():
    ok, reason = validate_username("jane_supervisor")
    assert ok is True
    assert reason is None


def test_validate_username_rejects_too_short():
    ok, reason = validate_username("ab")
    assert ok is False


def test_validate_username_rejects_too_long():
    ok, reason = validate_username("a" * 33)
    assert ok is False


def test_validate_username_accepts_boundary_lengths():
    assert validate_username("abc")[0] is True          # exactly minimum
    assert validate_username("a" * 32)[0] is True        # exactly maximum


def test_validate_username_rejects_empty_string():
    ok, _ = validate_username("")
    assert ok is False


def test_validate_username_rejects_spaces():
    ok, _ = validate_username("has space")
    assert ok is False


def test_validate_username_rejects_html_special_characters():
    # this is the exact shape of the DP-H2 attribute-breakout payload —
    # blocking it at creation time closes the gap at the source, not just
    # via output-escaping (defense in depth, not a replacement for it).
    ok, _ = validate_username('x" onmouseover="alert(1)')
    assert ok is False


def test_validate_username_allows_underscores_hyphens_and_periods():
    assert validate_username("first.last")[0] is True
    assert validate_username("first-last")[0] is True
    assert validate_username("first_last")[0] is True


# ── tokens ────────────────────────────────────────────────────────────────

def test_issue_and_verify_token_roundtrip():
    token = issue_token(SECRET, "alice", "supervisor")
    payload = verify_token(SECRET, token)
    assert payload["sub"] == "alice"
    assert payload["role"] == "supervisor"


def test_verify_token_rejects_wrong_secret():
    token = issue_token(SECRET, "alice", "supervisor")
    assert verify_token("a-different-secret-also-32-bytes-plus", token) is None


def test_verify_token_rejects_expired_token():
    token = issue_token(SECRET, "alice", "supervisor", ttl_seconds=-1)
    assert verify_token(SECRET, token) is None


def test_verify_token_rejects_garbage():
    assert verify_token(SECRET, "not.a.jwt") is None
    assert verify_token(SECRET, "") is None


def test_issue_token_rejects_invalid_role():
    with pytest.raises(ValueError):
        issue_token(SECRET, "alice", "root")


def test_issue_token_accepts_admin_role():
    token = issue_token(SECRET, "alice", "admin")
    assert verify_token(SECRET, token)["role"] == "admin"


def test_token_cannot_be_forged_with_alg_none():
    # classic JWT attack: an attacker crafts a token with alg=none and no
    # signature, hoping a lax verifier accepts it. verify_token must not.
    forged = pyjwt.encode({"sub": "alice", "role": "supervisor",
                            "iat": int(time.time()), "exp": int(time.time()) + 3600},
                           key="", algorithm="none")
    assert verify_token(SECRET, forged) is None


# ── token revocation (production-readiness: no server-side revocation) ─────

@pytest.mark.asyncio
async def test_revocation_store_in_memory_fallback_starts_with_nothing_revoked():
    store = RevocationStore()
    assert await store.is_revoked("alice", issued_at=int(time.time())) is False


@pytest.mark.asyncio
async def test_revocation_store_revoke_marks_earlier_tokens_revoked():
    store = RevocationStore()
    issued_at = int(time.time())
    await store.revoke("alice", now=issued_at + 10)
    assert await store.is_revoked("alice", issued_at=issued_at) is True


@pytest.mark.asyncio
async def test_revocation_store_does_not_revoke_tokens_issued_after_the_cutoff():
    store = RevocationStore()
    revoked_at = int(time.time())
    await store.revoke("alice", now=revoked_at)
    fresh_token_issued_at = revoked_at + 60  # e.g. a fresh login after the revoke
    assert await store.is_revoked("alice", issued_at=fresh_token_issued_at) is False


@pytest.mark.asyncio
async def test_revocation_store_only_affects_the_revoked_username():
    store = RevocationStore()
    issued_at = int(time.time())
    await store.revoke("alice", now=issued_at + 10)
    assert await store.is_revoked("bob", issued_at=issued_at) is False


@pytest.mark.asyncio
async def test_revocation_store_uses_redis_client_when_given():
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=None)
    fake_redis.get = AsyncMock(return_value=b"12345")
    store = RevocationStore(redis_client=fake_redis, ttl_seconds=43200)

    await store.revoke("alice", now=999)
    fake_redis.set.assert_called_once()
    call_args, call_kwargs = fake_redis.set.call_args
    assert call_args[0] == "floorwatch:revoked:alice"
    assert call_args[1] == 999
    assert call_kwargs.get("ex") == 43200

    assert await store.is_revoked("alice", issued_at=100) is True  # 100 <= 12345
    fake_redis.get.assert_called_once_with("floorwatch:revoked:alice")


@pytest.mark.asyncio
async def test_revocation_store_redis_no_key_means_not_revoked():
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    store = RevocationStore(redis_client=fake_redis)
    assert await store.is_revoked("alice", issued_at=100) is False


# ── shared secret file ────────────────────────────────────────────────────

def test_get_or_create_secret_persists_across_calls(tmp_path):
    path = tmp_path / "secret.txt"
    s1 = get_or_create_secret(path)
    s2 = get_or_create_secret(path)
    assert s1 == s2
    assert path.exists()


def test_get_or_create_secret_generates_nonempty_random_value(tmp_path):
    secret = get_or_create_secret(tmp_path / "secret.txt")
    assert len(secret) >= 32


# ── role hierarchy (make_auth_dependency) ──────────────────────────────────

def test_role_rank_ordering():
    assert ROLE_RANK["viewer"] < ROLE_RANK["supervisor"] < ROLE_RANK["admin"]
    assert ROLE_RANK["service"] == ROLE_RANK["viewer"]


@pytest.mark.asyncio
async def test_require_supervisor_accepts_admin_token():
    """Hierarchical check: an admin token satisfies a supervisor-required
    dependency — the whole point of admin being a superset, not a sibling."""
    dep = make_auth_dependency(SECRET, required_role="supervisor")
    token = issue_token(SECRET, "alice", "admin")
    payload = await dep(authorization=f"Bearer {token}")
    assert payload["role"] == "admin"


@pytest.mark.asyncio
async def test_require_supervisor_rejects_viewer_token():
    from fastapi import HTTPException
    dep = make_auth_dependency(SECRET, required_role="supervisor")
    token = issue_token(SECRET, "bob", "viewer")
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_rejects_supervisor_token():
    from fastapi import HTTPException
    dep = make_auth_dependency(SECRET, required_role="admin")
    token = issue_token(SECRET, "bob", "supervisor")
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_accepts_admin_token():
    dep = make_auth_dependency(SECRET, required_role="admin")
    token = issue_token(SECRET, "alice", "admin")
    payload = await dep(authorization=f"Bearer {token}")
    assert payload["role"] == "admin"


@pytest.mark.asyncio
async def test_make_auth_dependency_without_revocation_store_ignores_revocation():
    """Default behavior (revocation_store=None) is unchanged from before
    revocation existed — a structurally valid token is always accepted."""
    dep = make_auth_dependency(SECRET)
    token = issue_token(SECRET, "alice", "supervisor")
    payload = await dep(authorization=f"Bearer {token}")
    assert payload["sub"] == "alice"


@pytest.mark.asyncio
async def test_make_auth_dependency_rejects_revoked_token():
    from fastapi import HTTPException
    store = RevocationStore()
    token = issue_token(SECRET, "alice", "supervisor")
    await store.revoke("alice", now=int(time.time()) + 3600)  # well after this token's iat
    dep = make_auth_dependency(SECRET, revocation_store=store)
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_make_auth_dependency_accepts_fresh_token_after_revocation():
    """The point of revocation is killing existing sessions, not blocking
    the user from logging in again — a token issued AFTER the revoke must
    still work."""
    store = RevocationStore()
    await store.revoke("alice", now=int(time.time()) - 3600)  # revoked an hour ago
    fresh_token = issue_token(SECRET, "alice", "supervisor")   # issued now
    dep = make_auth_dependency(SECRET, revocation_store=store)
    payload = await dep(authorization=f"Bearer {fresh_token}")
    assert payload["sub"] == "alice"


@pytest.mark.asyncio
async def test_verify_ws_token_without_revocation_store_ignores_revocation():
    token = issue_token(SECRET, "alice", "supervisor")
    ws = SimpleNamespace(query_params={"token": token})
    payload = await verify_ws_token(SECRET, ws)
    assert payload["sub"] == "alice"


@pytest.mark.asyncio
async def test_verify_ws_token_rejects_revoked_token():
    store = RevocationStore()
    token = issue_token(SECRET, "alice", "supervisor")
    await store.revoke("alice", now=int(time.time()) + 3600)
    ws = SimpleNamespace(query_params={"token": token})
    payload = await verify_ws_token(SECRET, ws, revocation_store=store)
    assert payload is None


# ── UserStore ─────────────────────────────────────────────────────────────

def test_user_store_create_and_authenticate(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "s3cr3t-password", role="supervisor")
    result = store.authenticate("alice", "s3cr3t-password")
    assert result["role"] == "supervisor"


def test_user_store_authenticate_rejects_wrong_password(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "s3cr3t-password", role="supervisor")
    assert store.authenticate("alice", "wrong") is None


def test_user_store_authenticate_rejects_unknown_user(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.authenticate("nobody", "whatever") is None


def test_user_store_persists_to_disk(tmp_path):
    path = tmp_path / "users.json"
    store1 = UserStore(path)
    store1.create_user("bob", "hunter22", role="viewer")
    store2 = UserStore(path)  # fresh instance, same file
    assert store2.authenticate("bob", "hunter22")["role"] == "viewer"


def test_user_store_never_stores_plaintext_password(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path)
    store.create_user("carol", "super-secret-plaintext", role="supervisor")
    assert "super-secret-plaintext" not in path.read_text()


def test_user_store_list_and_exists(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor")
    assert store.user_exists("alice") is True
    assert store.user_exists("bob") is False
    assert store.list_usernames() == ["alice"]


def test_user_store_backward_compat_with_old_format_entry(tmp_path):
    """An account created before active/must_change_password/etc existed
    only ever had password_hash + role — must still authenticate and list
    correctly, defaulting the new fields sensibly."""
    import json
    path = tmp_path / "users.json"
    path.write_text(json.dumps({
        "old_user": {"password_hash": hash_password("oldpass123"), "role": "viewer"}
    }))
    store = UserStore(path)
    result = store.authenticate("old_user", "oldpass123")
    assert result == {"role": "viewer", "must_change_password": False}
    listed = store.list_users()
    assert listed[0]["active"] is True
    assert listed[0]["must_change_password"] is False


def test_user_store_new_account_defaults_must_change_password_true(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor", created_by="admin_bob")
    result = store.authenticate("alice", "pw12345")
    assert result["must_change_password"] is True
    listed = store.list_users()
    assert listed[0]["created_by"] == "admin_bob"


def test_user_store_deactivated_account_cannot_authenticate(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor")
    assert store.set_active("alice", False) is True
    assert store.authenticate("alice", "pw12345") is None


def test_user_store_reactivate_restores_login(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor")
    store.set_active("alice", False)
    store.set_active("alice", True)
    assert store.authenticate("alice", "pw12345") is not None


def test_user_store_set_active_unknown_user_returns_false(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.set_active("nobody", False) is False


def test_user_store_set_password_changes_credential_and_flag(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "oldpass123", role="supervisor", must_change_password=False)
    store.set_password("alice", "newpass456", must_change_password=True)
    assert store.authenticate("alice", "oldpass123") is None
    result = store.authenticate("alice", "newpass456")
    assert result["must_change_password"] is True


def test_user_store_mark_password_changed_clears_flag(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor")
    store.mark_password_changed("alice")
    result = store.authenticate("alice", "pw12345")
    assert result["must_change_password"] is False


def test_user_store_record_login_sets_timestamp(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345", role="supervisor")
    assert store.list_users()[0]["last_login_at"] is None
    store.record_login("alice")
    assert store.list_users()[0]["last_login_at"] is not None


def test_user_store_create_user_rejects_invalid_role(tmp_path):
    store = UserStore(tmp_path / "users.json")
    with pytest.raises(ValueError):
        store.create_user("alice", "pw12345", role="root")


def test_user_store_list_users_never_includes_password_hash(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "super-secret-pw", role="admin")
    listed = store.list_users()
    assert "password_hash" not in listed[0]


# ── account retention (DP-M4, DATA_PROTECTION_SECURITY_ANALYSIS.md) ───────

def test_user_store_deactivate_sets_deactivated_at(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    assert store.list_users()[0]["deactivated_at"] is None
    store.set_active("alice", False)
    assert store.list_users()[0]["deactivated_at"] is not None


def test_user_store_reactivate_clears_deactivated_at(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    store.set_active("alice", False)
    store.set_active("alice", True)
    assert store.list_users()[0]["deactivated_at"] is None


def test_user_store_delete_user_removes_account(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    assert store.delete_user("alice") is True
    assert store.user_exists("alice") is False


def test_user_store_delete_user_unknown_returns_false(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.delete_user("nobody") is False


def test_purge_stale_deactivated_accounts_removes_only_old_deactivated(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("still_active", "pw12345678", role="viewer")
    store.create_user("recently_deactivated", "pw12345678", role="viewer")
    store.create_user("long_deactivated", "pw12345678", role="viewer")
    store.set_active("recently_deactivated", False)
    store.set_active("long_deactivated", False)

    now = datetime.now(timezone.utc)
    # backdate long_deactivated's timestamp past the retention window
    users = store._load()
    users["long_deactivated"]["deactivated_at"] = (now - timedelta(days=100)).isoformat()
    store._save(users)

    result = purge_stale_deactivated_accounts(store, retention_days=90, now=now)

    assert result == {"purged": 1, "kept": 2}
    assert store.user_exists("still_active") is True
    assert store.user_exists("recently_deactivated") is True
    assert store.user_exists("long_deactivated") is False


def test_purge_stale_deactivated_accounts_never_touches_active_accounts(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    result = purge_stale_deactivated_accounts(store, retention_days=0)
    assert result == {"purged": 0, "kept": 1}
    assert store.user_exists("alice") is True


def test_purge_stale_deactivated_accounts_keeps_accounts_missing_timestamp(tmp_path):
    """An account deactivated before this field existed (old data) has no
    deactivated_at — must never be guessed away."""
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    store.set_active("alice", False)
    users = store._load()
    users["alice"]["deactivated_at"] = None
    store._save(users)

    result = purge_stale_deactivated_accounts(store, retention_days=0)
    assert result == {"purged": 0, "kept": 1}
    assert store.user_exists("alice") is True


def test_purge_stale_deactivated_accounts_dry_run_changes_nothing(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    store.set_active("alice", False)
    now = datetime.now(timezone.utc)
    users = store._load()
    users["alice"]["deactivated_at"] = (now - timedelta(days=100)).isoformat()
    store._save(users)

    result = purge_stale_deactivated_accounts(store, retention_days=90, now=now, dry_run=True)
    assert result == {"purged": 1, "kept": 0}
    assert store.user_exists("alice") is True  # dry run — nothing actually deleted


def test_purge_stale_deactivated_accounts_archives_before_deleting(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw12345678", role="viewer")
    store.set_active("alice", False)
    now = datetime.now(timezone.utc)
    users = store._load()
    users["alice"]["deactivated_at"] = (now - timedelta(days=100)).isoformat()
    store._save(users)

    archive_path = tmp_path / "archive" / "accounts.jsonl"
    purge_stale_deactivated_accounts(store, retention_days=90, now=now, archive_path=archive_path)

    assert archive_path.exists()
    assert "alice" in archive_path.read_text()
    assert store.user_exists("alice") is False


# ── PostgresUserStore (mocked — no real Postgres in this sandbox) ─────────

def _fake_psycopg_module(fake_conn):
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value.__enter__ = MagicMock(return_value=fake_conn)
    fake_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
    return fake_psycopg


def test_postgres_user_store_authenticate_success():
    password_hash = hash_password("pw12345")
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (password_hash, "supervisor", True, False)

    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        result = store.authenticate("alice", "pw12345")

    assert result == {"role": "supervisor", "must_change_password": False}


def test_postgres_user_store_authenticate_rejects_inactive_account():
    password_hash = hash_password("pw12345")
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (password_hash, "supervisor", False, False)

    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        result = store.authenticate("alice", "pw12345")

    assert result is None


def test_postgres_user_store_authenticate_unknown_user_returns_none():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None

    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        result = store.authenticate("nobody", "whatever")

    assert result is None


def test_postgres_user_store_create_user_runs_upsert():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        store.create_user("alice", "pw12345", role="admin", created_by="bob")

    call_args = fake_conn.execute.call_args_list[-1]
    assert "INSERT INTO floorwatch_users" in call_args[0][0]
    assert call_args[0][1][0] == "alice"


def test_postgres_user_store_create_user_rejects_invalid_role():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        with pytest.raises(ValueError):
            store.create_user("alice", "pw12345", role="root")


def test_postgres_user_store_schema_created_on_init():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresUserStore("postgresql://fake/dsn")

    schema_call = fake_conn.execute.call_args_list[0]
    assert "CREATE TABLE IF NOT EXISTS floorwatch_users" in schema_call[0][0]


def test_postgres_user_store_migrates_deactivated_at_column_on_init():
    """DP-M4: a table that already existed before deactivated_at was added
    needs this column backfilled explicitly — CREATE TABLE IF NOT EXISTS is
    a no-op against an existing table."""
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresUserStore("postgresql://fake/dsn")

    migration_call = fake_conn.execute.call_args_list[1]
    assert "ALTER TABLE floorwatch_users" in migration_call[0][0]
    assert "deactivated_at" in migration_call[0][0]


def test_postgres_user_store_set_active_false_sets_deactivated_at():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.rowcount = 1
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        result = store.set_active("alice", False)

    assert result is True
    call_args = fake_conn.execute.call_args_list[-1]
    assert "deactivated_at" in call_args[0][0]
    assert call_args[0][1][0] is False  # active=False passed through


def test_postgres_user_store_delete_user_runs_delete():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.rowcount = 1
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        result = store.delete_user("alice")

    assert result is True
    call_args = fake_conn.execute.call_args_list[-1]
    assert "DELETE FROM floorwatch_users" in call_args[0][0]
    assert call_args[0][1] == ("alice",)


def test_postgres_user_store_delete_user_unknown_returns_false():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.rowcount = 0
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresUserStore("postgresql://fake/dsn")
        assert store.delete_user("nobody") is False


# ── build_user_store dispatch ───────────────────────────────────────────

def test_build_user_store_uses_json_fallback_when_no_dsn(tmp_path):
    store = build_user_store(None, tmp_path / "users.json")
    assert isinstance(store, UserStore)


def test_build_user_store_falls_back_to_json_on_postgres_connection_failure(tmp_path):
    with patch("floorwatch_auth.PostgresUserStore", side_effect=Exception("connection refused")):
        store = build_user_store("postgresql://unreachable/dsn", tmp_path / "users.json")
    assert isinstance(store, UserStore)


def test_build_user_store_uses_postgres_when_reachable(tmp_path):
    fake_store = MagicMock()
    with patch("floorwatch_auth.PostgresUserStore", return_value=fake_store):
        store = build_user_store("postgresql://fake/dsn", tmp_path / "users.json")
    assert store is fake_store
