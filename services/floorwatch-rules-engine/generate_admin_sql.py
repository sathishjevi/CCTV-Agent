#!/usr/bin/env python3
"""
Prints ready-to-paste SQL to bootstrap the first admin account DIRECTLY in
Railway's Postgres query console, instead of running create_user.py against
the live container (railway run/shell). Useful when you'd rather manage the
first row yourself than depend on CLI access to the deployed service.

This does NOT connect to any database — it only computes the password hash
locally (same PBKDF2-HMAC-SHA256 format PostgresUserStore itself writes,
skills/lib/floorwatch_auth.py) and prints SQL text for you to run yourself
in Railway's dashboard (Postgres service -> Data/Query tab) or any other
Postgres client pointed at the same instance.

Usage:
  python generate_admin_sql.py alice
  python generate_admin_sql.py alice --role supervisor --password "..."   # non-interactive
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "lib"))
from floorwatch_auth import VALID_ROLES, hash_password  # noqa: E402

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS floorwatch_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    must_change_password BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT,
    last_login_at TIMESTAMPTZ
);"""


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description="Generate SQL to bootstrap a Floorwatch account directly in Postgres")
    parser.add_argument("username", help="Username for the account")
    parser.add_argument("--role", choices=sorted(VALID_ROLES - {"service"}), default="admin",
                         help="admin (default — this is what you want for the very first account), "
                              "supervisor, or viewer")
    parser.add_argument("--password", default=None,
                         help="Non-interactive password. Prompts via getpass if omitted (recommended — "
                              "avoids the real password ending up in shell history).")
    parser.add_argument("--must-change-password", dest="must_change", action="store_true", default=True,
                         help="Force a password change on first login (default: on)")
    parser.add_argument("--no-must-change-password", dest="must_change", action="store_false",
                         help="Skip the forced first-login password change")
    args = parser.parse_args()

    if args.password is not None:
        password = args.password
    else:
        password = getpass.getpass(f"Password for '{args.username}': ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    password_hash = hash_password(password)

    insert_sql = (
        "INSERT INTO floorwatch_users "
        "(username, password_hash, role, active, must_change_password) "
        f"VALUES ({_sql_quote(args.username)}, {_sql_quote(password_hash)}, "
        f"{_sql_quote(args.role)}, true, {'true' if args.must_change else 'false'}) "
        "ON CONFLICT (username) DO UPDATE SET "
        "password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, "
        "active = true, must_change_password = EXCLUDED.must_change_password;"
    )

    print("-- Paste this into Railway's Postgres query console (or psql/any Postgres")
    print("-- client) pointed at the SAME instance FLOORWATCH_POSTGRES_DSN uses.")
    print("-- Safe to run even if the table already exists (IF NOT EXISTS / ON CONFLICT).")
    print()
    print("-- 1. Ensure the table exists (harmless no-op if the service already created it)")
    print(SCHEMA_SQL)
    print()
    print(f"-- 2. Create/update '{args.username}' as role '{args.role}'")
    print(insert_sql)
    print()
    print("-- The password above is now hashed into the SQL text — the plaintext you typed",
          file=sys.stderr)
    print("-- is not stored anywhere by this script and does not appear in the printed SQL.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
