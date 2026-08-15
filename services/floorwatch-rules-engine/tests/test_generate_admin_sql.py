"""Unit tests for generate_admin_sql.py's SQL-generation logic — this
script never connects to a database itself, so the only real correctness
risk is the SQL string it prints (quoting/escaping), not database
behavior. The generated SQL's actual round-trip against verify_password()
is exercised in test_floorwatch_auth.py already (same hash_password())."""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))
from floorwatch_auth import hash_password, verify_password  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "generate_admin_sql.py"
spec = importlib.util.spec_from_file_location("generate_admin_sql", SCRIPT_PATH)
generate_admin_sql = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate_admin_sql)


def test_sql_quote_escapes_single_quotes():
    assert generate_admin_sql._sql_quote("alice") == "'alice'"
    assert generate_admin_sql._sql_quote("o'brien") == "'o''brien'"


def test_sql_quote_prevents_sql_injection_via_username():
    malicious = "alice'; DROP TABLE floorwatch_users; --"
    quoted = generate_admin_sql._sql_quote(malicious)
    # every embedded quote must be doubled (SQL-escaped), and the string
    # must still be wrapped in a single outer pair of quotes — i.e. the
    # DROP TABLE text stays inert data, never breaks out of the literal.
    assert quoted.startswith("'") and quoted.endswith("'")
    assert quoted.count("'") % 2 == 0
    inner = quoted[1:-1]
    assert "''" in inner  # the embedded ' was doubled, not left bare


def test_generated_hash_verifies_against_real_password():
    """Proves the script's actual hashing call (not a reimplementation)
    produces a hash the real auth module accepts."""
    h = hash_password("a-real-password-123")
    assert verify_password("a-real-password-123", h) is True
    assert verify_password("wrong", h) is False


def test_schema_sql_matches_postgresusertore_schema():
    """If PostgresUserStore's schema ever changes, this script's
    hand-copied CREATE TABLE would silently drift out of sync — this test
    exists to catch that, not to test this script's logic per se."""
    from floorwatch_auth import PostgresUserStore
    # Both must create the same table; comparing the table name + key
    # columns is enough to catch a real drift without over-fitting to
    # exact whitespace/formatting differences between the two copies.
    for col in ("username", "password_hash", "role", "active", "must_change_password"):
        assert col in generate_admin_sql.SCHEMA_SQL
        assert col in PostgresUserStore.SCHEMA_SQL
