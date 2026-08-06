#!/usr/bin/env python3
"""
Retention/rotation job — SECURITY_REVIEW.md M1 (see
floorwatch-rules-engine/retention.py for the shared rationale; this is
this service's half of the same policy, covering the data it owns).

Prunes two things older than --retention-days (default: config.RETENTION_DAYS):
  1. incident_notes.jsonl entries (archived before deletion, same as the
     rules engine's shift_digest.jsonl).
  2. Vector-store rows whose metadata.timestamp is older than the cutoff
     (vector_store.py's delete_before() — the vector store has no
     separate archive mechanism since it's a derived index re-buildable
     from shift_digest.jsonl/incident_notes.jsonl by ingest.py; the
     JSONL files are the archival source of truth, not the index).

Usage:
  python retention.py
  python retention.py --retention-days 30
  python retention.py --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "skills" / "lib"))
import config  # noqa: E402
from floorwatch_retention import prune_jsonl_file  # noqa: E402
from vector_store import build_vector_store  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Floorwatch intelligence-service retention/rotation job")
    parser.add_argument("--retention-days", type=int, default=None,
                        help="Override config.RETENTION_DAYS")
    parser.add_argument("--archive-dir", type=str, default=None,
                        help="Directory to archive pruned incident notes to before deletion (default: ./archive/)")
    parser.add_argument("--no-archive", action="store_true", help="Prune notes without archiving first")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be pruned, change nothing")
    args = parser.parse_args()

    retention_days = args.retention_days if args.retention_days is not None else config.RETENTION_DAYS
    archive_dir = None if args.no_archive else (
        Path(args.archive_dir) if args.archive_dir else config.SERVICE_DIR / "archive")

    notes_result = prune_jsonl_file(config.INCIDENT_NOTES_PATH, retention_days, archive_dir=archive_dir,
                                     dry_run=args.dry_run, archive_prefix="incident_notes_archived")

    verb = "Would prune" if args.dry_run else "Pruned"
    print(f"{verb} {notes_result['pruned']} incident note(s) older than {retention_days} days "
          f"from {config.INCIDENT_NOTES_PATH}")
    print(f"Kept {notes_result['kept']} note(s) ({notes_result['undated']} undated — always kept)")

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    if args.dry_run:
        print(f"Vector store: dry-run, would delete rows with metadata.timestamp < {cutoff_iso}")
    else:
        vector_store = build_vector_store(config)
        deleted = vector_store.delete_before(cutoff_iso)
        print(f"Vector store: deleted {deleted} row(s) with metadata.timestamp < {cutoff_iso}")


if __name__ == "__main__":
    main()
