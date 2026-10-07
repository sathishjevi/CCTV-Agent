"""Password hashing: scrypt for new hashes, old PBKDF2 hashes still verify and
are upgraded the next time their owner logs in."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_auth as auth  # noqa: E402


def test_production_parameters_are_the_owasp_recommended_scrypt_row():
    # The suite runs with a cheaper cost (conftest.py); this pins what ships.
    assert auth.PRODUCTION_SCRYPT_PARAMS == (2 ** 15, 8, 3)


def test_one_real_strength_hash_round_trips(monkeypatch):
    n, r, p = auth.PRODUCTION_SCRYPT_PARAMS
    monkeypatch.setattr(auth, "SCRYPT_N", n)
    monkeypatch.setattr(auth, "SCRYPT_P", p)
    stored = auth.hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$32768$8$3$")
    assert auth.verify_password("correct horse battery staple", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.password_needs_rehash(stored)


def test_hashes_are_salted_so_equal_passwords_differ():
    assert auth.hash_password("same-password-1") != auth.hash_password("same-password-1")


def test_legacy_pbkdf2_hashes_still_verify_and_are_flagged_for_upgrade():
    legacy = auth._legacy_pbkdf2_hash("old-account-password")
    assert "scrypt" not in legacy
    assert auth.verify_password("old-account-password", legacy)
    assert not auth.verify_password("nope", legacy)
    assert auth.password_needs_rehash(legacy)


def test_weaker_scrypt_parameters_are_flagged_for_upgrade(monkeypatch):
    stored = auth.hash_password("some-password-12")
    monkeypatch.setattr(auth, "SCRYPT_N", auth.SCRYPT_N * 2)   # the standard moves up later
    assert auth.verify_password("some-password-12", stored)     # old hashes keep working
    assert auth.password_needs_rehash(stored)                    # and get re-hashed on next login


@pytest.mark.parametrize("bad", [
    "", "garbage", "scrypt$", "scrypt$1$2$3$4$5", "zz$zz", "scrypt$32768$8$3$nothex$nothex",
    "scrypt$%d$8$3$00$00" % (2 ** 30),             # absurd cost must not be attempted
    "scrypt$32768$8$9999$00112233445566778899aabbccddeeff$00" * 1,
])
def test_corrupt_or_hostile_stored_values_fail_safely(bad):
    assert auth.verify_password("anything", bad) is False


def test_non_string_inputs_never_raise():
    assert auth.verify_password("x", None) is False
    assert auth.verify_password(None, "scrypt$1024$8$1$00$00") is False


def test_overlong_passwords_are_rejected_before_any_work_is_done():
    stored = auth.hash_password("normal-password-1")
    assert auth.verify_password("a" * (auth.MAX_PASSWORD_LENGTH + 1), stored) is False


def test_dummy_verification_is_always_false_and_does_not_raise():
    assert auth.verify_password_dummy("whatever") is False


# ── upgrade on login ────────────────────────────────────────────────────────

def _json_store_with_legacy_account(tmp_path, password="legacy-password-1"):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"alice": {
        "password_hash": auth._legacy_pbkdf2_hash(password), "role": "supervisor",
        "active": True, "must_change_password": False}}))
    return auth.UserStore(path), path


def test_a_successful_login_upgrades_a_legacy_hash_in_the_json_store(tmp_path):
    store, path = _json_store_with_legacy_account(tmp_path)
    assert auth.password_needs_rehash(json.loads(path.read_text())["alice"]["password_hash"])

    assert store.authenticate("alice", "legacy-password-1") == {"role": "supervisor", "must_change_password": False}

    upgraded = json.loads(path.read_text())["alice"]["password_hash"]
    assert upgraded.startswith("scrypt$") and not auth.password_needs_rehash(upgraded)
    assert store.authenticate("alice", "legacy-password-1") is not None   # still logs in afterwards


def test_a_failed_login_does_not_touch_the_stored_hash(tmp_path):
    store, path = _json_store_with_legacy_account(tmp_path)
    before = path.read_text()
    assert store.authenticate("alice", "wrong-password") is None
    assert path.read_text() == before


def test_unknown_and_deactivated_accounts_still_fail(tmp_path):
    store, path = _json_store_with_legacy_account(tmp_path)
    assert store.authenticate("nobody", "legacy-password-1") is None
    store.set_active("alice", False)
    assert store.authenticate("alice", "legacy-password-1") is None


def test_the_postgres_store_upgrades_on_login_too():
    """No Postgres here, so drive PostgresUserStore.authenticate with a stub
    connection: same code path, fake rows."""
    legacy = auth._legacy_pbkdf2_hash("pg-password-123")
    updates = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("SELECT"):
                class R:
                    def fetchone(_): return (legacy, "admin", True, False)
                return R()
            updates.append((sql, params))

    store = auth.PostgresUserStore.__new__(auth.PostgresUserStore)
    store._connect = lambda: Conn()

    assert store.authenticate("bob", "pg-password-123") == {"role": "admin", "must_change_password": False}
    assert len(updates) == 1
    sql, params = updates[0]
    assert "UPDATE floorwatch_users SET password_hash" in sql
    assert params[1] == "bob" and params[0].startswith("scrypt$")
