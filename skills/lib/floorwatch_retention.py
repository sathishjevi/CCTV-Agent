"""Shared retention/rotation helper — SECURITY_REVIEW.md M1: "No
retention/expiry policy — history grows unbounded forever... Decide an
explicit retention window... and implement rotation/deletion."

Shared (not forked per service) because both `floorwatch-rules-engine`
(shift_digest.jsonl) and `floorwatch-intelligence` (incident_notes.jsonl)
have the same append-only-JSONL-with-a-timestamp-field shape and need the
same pruning behavior — matching this project's existing pattern of
shared code living in skills/lib rather than duplicated per service (see
floorwatch_schema.py, floorwatch_auth.py).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def parse_timestamp(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def prune_jsonl_file(path: Path, retention_days: int, archive_dir: Optional[Path] = None,
                      dry_run: bool = False, timestamp_field: str = "timestamp",
                      now: Optional[datetime] = None, archive_prefix: str = "archived") -> dict:
    """Prune JSONL entries older than retention_days, based on
    entry[timestamp_field]. Entries with a missing/unparseable timestamp
    are always kept — never guessed away, since we can't know their age.
    Malformed (non-JSON) lines are also always kept, for the same reason.

    If archive_dir is given (and dry_run is False), pruned lines are
    appended to a dated file there BEFORE being removed from the source
    file — data is archived, never silently destroyed.

    Returns {"kept": N, "pruned": N, "undated": N}.
    """
    if not path.exists():
        return {"kept": 0, "pruned": 0, "undated": 0}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    kept, pruned = [], []
    undated = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue

        ts = parse_timestamp(entry.get(timestamp_field, ""))
        if ts is None:
            kept.append(line)
            undated += 1
            continue

        if ts < cutoff:
            pruned.append(line)
        else:
            kept.append(line)

    if dry_run:
        return {"kept": len(kept), "pruned": len(pruned), "undated": undated}

    if pruned and archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{archive_prefix}_{now:%Y%m%d}.jsonl"
        with open(archive_path, "a", encoding="utf-8") as f:
            f.write("\n".join(pruned) + "\n")

    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return {"kept": len(kept), "pruned": len(pruned), "undated": undated}
