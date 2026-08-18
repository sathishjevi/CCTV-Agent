#!/usr/bin/env python3
"""
Retention/rotation job — SECURITY_REVIEW.md M1: "No retention/expiry
policy — history grows unbounded forever... Decide an explicit retention
window (the brief never specifies one) and implement rotation/deletion."

Prunes shift_digest.jsonl entries, stale deactivated accounts, AND
event_history entries — the first two older than --retention-days
(default: config.RETENTION_DAYS, 90 — see config.py for the rationale),
event_history older than --event-history-retention-days (default:
config.EVENT_HISTORY_RETENTION_DAYS, 30 — a tighter default since it's
much higher-volume). Pruned entries/accounts are archived to a dated
JSONL file before deletion by default — never silently destroyed.

The account half is DP-M4 (DATA_PROTECTION_SECURITY_ANALYSIS.md): this
job used to only ever touch shift_digest.jsonl — a deactivated account's
record (and any personal data in created_by/last_login_at) persisted
forever with no expiry path. Only accounts an admin has explicitly
deactivated are ever eligible, and only once they've sat deactivated past
the retention window — an active account is never touched regardless of
age. See floorwatch_auth.py's purge_stale_deactivated_accounts().

The event_history half closes a gap reported directly: a supervisor
reviewed and confirmed a flagged task, watched it happen live in the
dashboard, and it was never durably recorded anywhere — shift_digest.jsonl
only ever captured zone_escalated/task_flag. See event_history.py's
module docstring for the full picture; this job is what keeps that new,
much-higher-volume table from growing unbounded forever.

Meant to run on a schedule (cron/Task Scheduler/Celery Beat), same as
shift_digest_job.py — see that script's docstring for why this project
uses plain scripts rather than standing up unused Celery scaffolding.

Usage:
  python retention.py                         # prune using config.RETENTION_DAYS
  python retention.py --retention-days 30      # override
  python retention.py --event-history-retention-days 14  # override just this one
  python retention.py --dry-run                # report what would be pruned, change nothing
  python retention.py --no-archive              # prune without archiving first
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "skills" / "lib"))
import config  # noqa: E402
from floorwatch_retention import prune_jsonl_file  # noqa: E402
from floorwatch_auth import build_user_store, purge_stale_deactivated_accounts  # noqa: E402
from event_history import build_event_history_store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Floorwatch shift-digest retention/rotation job")
    parser.add_argument("--retention-days", type=int, default=None,
                        help="Override config.RETENTION_DAYS")
    parser.add_argument("--event-history-retention-days", type=int, default=None,
                        help="Override config.EVENT_HISTORY_RETENTION_DAYS")
    parser.add_argument("--digest-path", type=str, default=None)
    parser.add_argument("--archive-dir", type=str, default=None,
                        help="Directory to archive pruned entries to before deletion (default: ./archive/)")
    parser.add_argument("--no-archive", action="store_true", help="Prune without archiving first")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be pruned, change nothing")
    args = parser.parse_args()

    retention_days = args.retention_days if args.retention_days is not None else config.RETENTION_DAYS
    digest_path = Path(args.digest_path) if args.digest_path else config.DIGEST_PATH
    archive_dir = None if args.no_archive else (
        Path(args.archive_dir) if args.archive_dir else config.SERVICE_DIR / "archive")

    result = prune_jsonl_file(digest_path, retention_days, archive_dir=archive_dir,
                               dry_run=args.dry_run, archive_prefix="shift_digest_archived")

    verb = "Would prune" if args.dry_run else "Pruned"
    print(f"{verb} {result['pruned']} entries older than {retention_days} days from {digest_path}")
    print(f"Kept {result['kept']} entries ({result['undated']} had no parseable timestamp — always kept)")
    if result["pruned"] and archive_dir and not args.dry_run:
        print(f"Archived pruned entries under {archive_dir}/")

    # DP-M4 — accounts deactivated (not just any old account) past the same
    # retention window. Uses whichever store main.py itself uses (Postgres
    # if configured, else the local JSON file).
    users = build_user_store(config.POSTGRES_DSN, config.USERS_PATH)
    accounts_dir = archive_dir if archive_dir else config.SERVICE_DIR / "archive"
    accounts_archive_path = None if args.no_archive else (
        accounts_dir / f"accounts_archived_{datetime.now(timezone.utc):%Y%m%d}.jsonl")
    account_result = purge_stale_deactivated_accounts(
        users, retention_days, archive_path=accounts_archive_path, dry_run=args.dry_run)

    print(f"{verb} {account_result['purged']} accounts deactivated more than {retention_days} days ago")
    print(f"Kept {account_result['kept']} accounts (active, recently deactivated, or missing a "
          f"deactivated_at timestamp)")
    if account_result["purged"] and accounts_archive_path and not args.dry_run:
        print(f"Archived purged accounts to {accounts_archive_path}")

    # Event history — the full zone/task lifecycle audit trail (see
    # event_history.py's module docstring). Separate, shorter default
    # retention window than the digest/accounts above, since this table
    # is much higher-volume.
    event_history_retention_days = (
        args.event_history_retention_days if args.event_history_retention_days is not None
        else config.EVENT_HISTORY_RETENTION_DAYS)
    event_history = build_event_history_store(config.POSTGRES_DSN, config.EVENT_HISTORY_PATH)
    events_dir = archive_dir if archive_dir else config.SERVICE_DIR / "archive"
    events_archive_dir = None if args.no_archive else events_dir
    event_result = event_history.purge_older_than(
        event_history_retention_days, archive_dir=events_archive_dir,
        archive_prefix="event_history_archived", dry_run=args.dry_run)

    print(f"{verb} {event_result['purged']} event_history entries older than "
          f"{event_history_retention_days} days")
    print(f"Kept {event_result['kept']} event_history entries")
    if event_result["purged"] and events_archive_dir and not args.dry_run:
        print(f"Archived purged event_history entries under {events_archive_dir}/")


if __name__ == "__main__":
    main()
