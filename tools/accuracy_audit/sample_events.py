#!/usr/bin/env python3
"""
Accuracy-audit sampling harness — build brief Phase 4 task 1:
"Build a lightweight accuracy-audit harness: sample detection events
across different times of day / lighting / camera angles and log them
for manual comparison against ground truth."

Reads the shift-digest JSONL log(s) the rules engine already writes
(services/floorwatch-rules-engine/shift_digest.jsonl — Tier 3 escalations
and roster-ignored gaps for Part B, task_flag for Part A) and produces a
stratified sample for a human reviewer to mark against what actually
happened.

Stratification proxy: this pilot's event schema has no separate "lighting
condition" field, and one physical camera == one "angle" — so this harness
stratifies by (camera_id, hour-of-day bucket) as the closest available
proxy for "different times of day / lighting / camera angles". Flagged
here and in the README rather than silently treated as equivalent.

Usage:
  python sample_events.py --digest ../../services/floorwatch-rules-engine/shift_digest.jsonl \
      --per-stratum 5 --out review_sample.csv
"""

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOUR_BUCKETS = [
    (0, 6, "night"),
    (6, 11, "morning"),
    (11, 17, "afternoon"),
    (17, 21, "evening"),
    (21, 24, "late_night"),
]


def hour_bucket(timestamp: str) -> str:
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "unknown"
    hour = ts.hour
    for start, end, label in HOUR_BUCKETS:
        if start <= hour < end:
            return label
    return "unknown"


def load_events(digest_paths: list) -> list:
    events = []
    for path in digest_paths:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def stratify(events: list) -> dict:
    strata = defaultdict(list)
    for evt in events:
        camera_id = evt.get("camera_id", "unknown")
        bucket = hour_bucket(evt.get("timestamp", ""))
        strata[(camera_id, bucket)].append(evt)
    return strata


def sample(strata: dict, per_stratum: int, seed: int) -> list:
    rng = random.Random(seed)
    sampled = []
    for (camera_id, bucket), evts in sorted(strata.items()):
        chosen = rng.sample(evts, min(per_stratum, len(evts)))
        for evt in chosen:
            sampled.append({
                "camera_id": camera_id,
                "hour_bucket": bucket,
                "event_id": evt.get("event_id", ""),
                "timestamp": evt.get("timestamp", ""),
                "zone_id": evt.get("zone_id", ""),
                "event_type": evt.get("event_type", ""),
                "confidence": evt.get("confidence", ""),
                "message": evt.get("message", ""),
                # left blank for the human reviewer:
                "ground_truth": "",   # TP | FP | FN | unclear
                "reviewer_notes": "",
            })
    return sampled


def write_csv(rows: list, out_path: Path):
    fieldnames = ["camera_id", "hour_bucket", "event_id", "timestamp", "zone_id",
                  "event_type", "confidence", "message", "ground_truth", "reviewer_notes"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Floorwatch accuracy-audit sampling harness")
    parser.add_argument("--digest", nargs="+", required=True,
                        help="One or more shift_digest.jsonl paths to sample from")
    parser.add_argument("--per-stratum", type=int, default=5,
                        help="Max samples per (camera_id, hour-of-day-bucket) stratum")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible sampling")
    parser.add_argument("--out", type=str, default="review_sample.csv")
    args = parser.parse_args()

    events = load_events(args.digest)
    if not events:
        print(f"No events found in {args.digest} — nothing to sample. "
              f"(This is expected if no shift has run yet in this environment.)")
        return

    strata = stratify(events)
    rows = sample(strata, args.per_stratum, args.seed)
    write_csv(rows, Path(args.out))

    print(f"Sampled {len(rows)} events across {len(strata)} (camera_id, hour_bucket) strata "
          f"from {len(events)} total logged events.")
    print(f"Wrote review sheet to {args.out} — fill in 'ground_truth' (TP/FP/FN/unclear) "
          f"per row, then run compute_accuracy.py on it.")


if __name__ == "__main__":
    main()
