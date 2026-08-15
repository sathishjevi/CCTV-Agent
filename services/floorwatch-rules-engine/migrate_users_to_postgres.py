#!/usr/bin/env python3
"""
One-off migration: copies accounts from the local users.json fallback
into Postgres, for a deployment switching FLOORWATCH_POSTGRES_DSN on for
the first time after already having accounts in the JSON file (e.g. an
existing Railway deployment created via create_user.py before Postgres
was configured).

Safe to run more than once — existing Postgres accounts are left alone
unless --overwrite is passed (uses the same ON CONFLICT DO UPDATE as
PostgresUserStore.create_user, so overwriting just re-applies whatever's
still in users.json, it doesn't invent new values).

Usage:
  python migrate_users_to_postgres.py                # uses FLOORWATCH_POSTGRES_DSN from config
  python migrate_users_to_postgres.py --dry-run       # show what would move, change nothing
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "lib"))

import config  # noqa: E402
from floorwatch_auth import PostgresUserStore  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Migrate users.json accounts into Postgres")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated, change nothing")
    args = parser.parse_args()

    if not config.POSTGRES_DSN:
        print("FLOORWATCH_POSTGRES_DSN is not set — nothing to migrate into. "
              "Set it first, then re-run this script.", file=sys.stderr)
        sys.exit(1)

    if not config.USERS_PATH.exists():
        print(f"No local users.json found at {config.USERS_PATH} — nothing to migrate.")
        return

    try:
        raw_users = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"users.json at {config.USERS_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not raw_users:
        print(f"users.json at {config.USERS_PATH} has no accounts — nothing to migrate.")
        return

    print(f"Found {len(raw_users)} account(s) in {config.USERS_PATH}:")
    for username, record in raw_users.items():
        print(f"  {username} (role={record.get('role', 'viewer')})")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    store = PostgresUserStore(config.POSTGRES_DSN)
    migrated = 0
    for username, record in raw_users.items():
        # Writes the pre-hashed password directly (not through create_user,
        # which would re-hash a plaintext password we don't have) — same
        # PBKDF2 format either store produces, so this is a straight copy,
        # not a re-derivation.
        import psycopg
        with psycopg.connect(config.POSTGRES_DSN, autocommit=True, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO floorwatch_users "
                "(username, password_hash, role, active, must_change_password, created_at, created_by, last_login_at) "
                "VALUES (%s,%s,%s,%s,%s, COALESCE(%s, now()), %s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (
                    username, record.get("password_hash", ""), record.get("role", "viewer"),
                    record.get("active", True), record.get("must_change_password", False),
                    record.get("created_at"), record.get("created_by"), record.get("last_login_at"),
                ),
            )
        migrated += 1

    print(f"\nMigrated {migrated} account(s) into Postgres.")
    print("Once you've confirmed logins work against Postgres, the local "
          f"{config.USERS_PATH} file is no longer read (build_user_store() "
          "prefers Postgres whenever FLOORWATCH_POSTGRES_DSN is set) — safe "
          "to leave in place or delete.")


if __name__ == "__main__":
    main()
