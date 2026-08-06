#!/usr/bin/env python3
"""
Shift-digest summarization job — build brief Phase 4 task 5: "Build the
shift-digest job (scheduled, e.g. cron or Celery beat) that summarizes
the day's gap/flag events, tags patterns (recurring same zone/time vs.
one-off), and stores/exports the digest."

This is a plain script, not a Celery task — this repo already has Celery
infrastructure (docker/*, src/face_detection/celeryconfig.py) for the
detection pipeline, but no broker is actually running in this dev sandbox
(the Redis used elsewhere in this phase is a fakeredis stand-in, not
real Celery infra). Rather than standing up unused Celery scaffolding,
this is written to be trivially wrapped in a Celery Beat task OR invoked
by plain cron/Windows Task Scheduler — see "Scheduling" below.

What it does:
  1. Reads the shift digest (shift_digest.jsonl — Tier 3 escalations,
     roster-ignored gaps, and task_flag events the rules engine already
     writes durably).
  2. Filters to a target day (default: today).
  3. Tags each (zone_id, hour-of-day-bucket) group as "recurring" (seen
     on >=2 distinct calendar dates across the whole digest history) vs.
     "one-off" (only ever seen on the target day) — the brief's explicit
     ask, using the same hour-of-day-bucket proxy as
     tools/accuracy_audit/sample_events.py for consistency.
  4. Writes a structured JSON summary AND a human-readable Markdown
     report to shift_digests/<date>.json / .md.

Usage:
  python shift_digest_job.py                       # today, using shift_digest.jsonl
  python shift_digest_job.py --date 2026-07-24
  python shift_digest_job.py --digest shift_digest.jsonl --out-dir shift_digests

Scheduling:
  cron (Linux/macOS), end of every shift, e.g. 23:55 daily:
    55 23 * * * cd /path/to/floorwatch-rules-engine && python shift_digest_job.py

  Windows Task Scheduler: create a daily trigger running
    python.exe C:\\path\\to\\floorwatch-rules-engine\\shift_digest_job.py

  Celery Beat (if/when this repo's existing Celery infra is wired to this
  service): wrap main()'s body in a @app.task and add a beat_schedule
  entry — no logic here needs to change.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools" / "accuracy_audit"))
from sample_events import hour_bucket  # noqa: E402 — same time-of-day proxy used for accuracy sampling

SERVICE_DIR = Path(__file__).resolve().parent


def load_digest(path: Path) -> list:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def event_date(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        return "unknown"


def summarize(events: list, target_date: str) -> dict:
    todays = [e for e in events if event_date(e.get("timestamp", "")) == target_date]

    by_event_type = defaultdict(int)
    by_zone = defaultdict(int)
    for e in todays:
        by_event_type[e.get("event_type", "unknown")] += 1
        by_zone[e.get("zone_id", "unknown")] += 1

    # Which (zone, hour_bucket) groups has this ever happened in, across
    # ALL history (not just today) — used to tag recurrence.
    all_dates_by_group = defaultdict(set)
    for e in events:
        key = (e.get("zone_id", "unknown"), hour_bucket(e.get("timestamp", "")))
        d = event_date(e.get("timestamp", ""))
        if d != "unknown":
            all_dates_by_group[key].add(d)

    todays_groups = defaultdict(list)
    for e in todays:
        key = (e.get("zone_id", "unknown"), hour_bucket(e.get("timestamp", "")))
        todays_groups[key].append(e)

    patterns = []
    for (zone_id, bucket), group_events in sorted(todays_groups.items()):
        distinct_dates = all_dates_by_group[(zone_id, bucket)]
        pattern = "recurring" if len(distinct_dates) >= 2 else "one_off"
        patterns.append({
            "zone_id": zone_id,
            "hour_bucket": bucket,
            "pattern": pattern,
            "distinct_dates_seen": len(distinct_dates),
            "event_count_today": len(group_events),
            "event_types_today": sorted({e.get("event_type", "unknown") for e in group_events}),
        })

    return {
        "date": target_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_events_today": len(todays),
        "by_event_type": dict(by_event_type),
        "by_zone": dict(by_zone),
        "patterns": patterns,
        "recurring_pattern_count": sum(1 for p in patterns if p["pattern"] == "recurring"),
        "one_off_pattern_count": sum(1 for p in patterns if p["pattern"] == "one_off"),
    }


def to_markdown(summary: dict) -> str:
    lines = [
        f"# Floorwatch Shift Digest — {summary['date']}",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        f"**Total events**: {summary['total_events_today']}",
        "",
        "## By event type",
        "",
    ]
    for et, count in sorted(summary["by_event_type"].items()):
        lines.append(f"- `{et}`: {count}")
    lines += ["", "## By zone", ""]
    for zone, count in sorted(summary["by_zone"].items()):
        lines.append(f"- {zone}: {count}")
    lines += [
        "",
        f"## Patterns ({summary['recurring_pattern_count']} recurring, "
        f"{summary['one_off_pattern_count']} one-off)",
        "",
        "| Zone | Time bucket | Pattern | Seen on N days | Events today | Types |",
        "|---|---|---|---|---|---|",
    ]
    for p in summary["patterns"]:
        lines.append(
            f"| {p['zone_id']} | {p['hour_bucket']} | {p['pattern']} | "
            f"{p['distinct_dates_seen']} | {p['event_count_today']} | "
            f"{', '.join(p['event_types_today'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Floorwatch shift-digest summarization job")
    parser.add_argument("--digest", type=str, default=str(SERVICE_DIR / "shift_digest.jsonl"))
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD; defaults to today (UTC)")
    parser.add_argument("--out-dir", type=str, default=str(SERVICE_DIR / "shift_digests"))
    args = parser.parse_args()

    target_date = args.date or datetime.now(timezone.utc).date().isoformat()
    events = load_digest(Path(args.digest))
    summary = summarize(events, target_date)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{target_date}.json"
    md_path = out_dir / f"{target_date}.md"
    json_path.write_text(json.dumps(summary, indent=2))
    md_path.write_text(to_markdown(summary))

    print(f"Shift digest for {target_date}: {summary['total_events_today']} events, "
          f"{summary['recurring_pattern_count']} recurring pattern(s), "
          f"{summary['one_off_pattern_count']} one-off.")
    print(f"Wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
