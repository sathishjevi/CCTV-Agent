"""Unit tests for the shared skills/lib/floorwatch_auth.py module."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402

from floorwatch_auth import (  # noqa: E402
    UserStore, hash_password, verify_password, issue_token, verify_token,
    get_or_create_secret,
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


def test_token_cannot_be_forged_with_alg_none():
    # classic JWT attack: an attacker crafts a token with alg=none and no
    # signature, hoping a lax verifier accepts it. verify_token must not.
    forged = pyjwt.encode({"sub": "alice", "role": "supervisor",
                            "iat": int(time.time()), "exp": int(time.time()) + 3600},
                           key="", algorithm="none")
    assert verify_token(SECRET, forged) is None


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


# ── UserStore ─────────────────────────────────────────────────────────────

def test_user_store_create_and_authenticate(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "s3cr3t-password", role="supervisor")
    assert store.authenticate("alice", "s3cr3t-password") == "supervisor"


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
    assert store2.authenticate("bob", "hunter22") == "viewer"


def test_user_store_never_stores_plaintext_password(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path)
    store.create_user("carol", "super-secret-plaintext", role="supervisor")
    assert "super-secret-plaintext" not in path.read_text()


def test_user_store_list_and_exists(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create_user("alice", "pw", role="supervisor")
    assert store.user_exists("alice") is True
    assert store.user_exists("bob") is False
    assert store.list_usernames() == ["alice"]
