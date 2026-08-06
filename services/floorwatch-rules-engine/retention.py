#!/usr/bin/env python3
"""
Retention/rotation job — SECURITY_REVIEW.md M1: "No retention/expiry
policy — history grows unbounded forever... Decide an explicit retention
window (the brief never specifies one) and implement rotation/deletion."

Prunes shift_digest.jsonl entries older than --retention-days (default:
config.RETENTION_DAYS, 90 — see config.py for the rationale). Pruned
entries are archived to a dated JSONL file before deletion by default —
never silently destroyed.

Meant to run on a schedule (cron/Task Scheduler/Celery Beat), same as
shift_digest_job.py — see that script's docstring for why this project
uses plain scripts rather than standing up unused Celery scaffolding.

Usage:
  python retention.py                         # prune using config.RETENTION_DAYS
  python retention.py --retention-days 30      # override
  python retention.py --dry-run                # report what would be pruned, change nothing
  python retention.py --no-archive              # prune without archiving first
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "skills" / "lib"))
import config  # noqa: E402
from floorwatch_retention import prune_jsonl_file  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Floorwatch shift-digest retention/rotation job")
    parser.add_argument("--retention-days", type=int, default=None,
                        help="Override config.RETENTION_DAYS")
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


if __name__ == "__main__":
    main()
