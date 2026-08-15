#!/usr/bin/env python3
"""
Bootstrap / manage Floorwatch supervisor and viewer accounts.

Deliberately NOT bundled with any default username/password in the repo —
a checked-in credential is a checked-in secret, and would defeat the point
of adding authentication (see SECURITY_REVIEW.md, finding AUTH-1). Run
this once before starting the rules engine to create the first account.

Usage:
  python create_user.py alice --role admin        # bootstrap the first account — see note below
  python create_user.py bob --role supervisor
  python create_user.py carol --role viewer
  python create_user.py --list
  python create_user.py svc --role viewer --password "..."   # non-interactive, e.g. container entrypoints

Once at least one admin account exists, prefer creating further accounts
from the dashboard's "Manage Users" screen (admin-only) instead of this
script — this CLI still exists for the initial bootstrap (nobody can grant
themselves admin access from the UI before an admin account exists) and
for direct server access if the UI is ever unreachable.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "lib"))

import config  # noqa: E402
from floorwatch_auth import VALID_ROLES, build_user_store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Manage Floorwatch user accounts")
    parser.add_argument("username", nargs="?", help="Username to create/update")
    parser.add_argument("--role", choices=sorted(VALID_ROLES - {"service"}), default="supervisor",
                        help="admin (manages accounts + full access), supervisor (full read/write), "
                             "or viewer (read-only)")
    parser.add_argument("--list", action="store_true", help="List existing usernames and exit")
    parser.add_argument("--password", default=None,
                        help="Non-interactive password (e.g. for container entrypoints/CI). "
                             "Prompts interactively via getpass if omitted (recommended for humans).")
    args = parser.parse_args()

    # Uses the SAME store the running service uses (Postgres if
    # FLOORWATCH_POSTGRES_DSN is set) — critical for bootstrap: creating
    # the first admin in the local JSON fallback while the service itself
    # is configured for Postgres would create an account the service never
    # actually sees.
    store = build_user_store(config.POSTGRES_DSN, config.USERS_PATH)

    if args.list:
        usernames = store.list_usernames()
        if not usernames:
            print(f"No users yet in {config.USERS_PATH}")
        else:
            print(f"Users in {config.USERS_PATH}:")
            for u in usernames:
                print(f"  {u}")
        return

    if not args.username:
        parser.error("username is required unless --list is given")

    if store.user_exists(args.username):
        confirm = input(f"User '{args.username}' already exists — overwrite password/role? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    if args.password is not None:
        password = args.password
    else:
        password = getpass.getpass(f"Password for '{args.username}': ")
        confirm_password = getpass.getpass("Confirm password: ")
        if password != confirm_password:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    store.create_user(args.username, password, role=args.role)
    print(f"Created/updated '{args.username}' as role '{args.role}' in {config.USERS_PATH}")


if __name__ == "__main__":
    main()
