#!/usr/bin/env python3
"""
Computes accuracy stats from a completed review sheet (produced by
sample_events.py, ground_truth column filled in by a human reviewer).

Used standalone for a human-readable report, and by go_live_checklist.py
to gate real-notification go-live on a false-positive-rate target.

ground_truth values expected: TP (true positive — the flagged event was
real), FP (false positive — flagged but nothing was actually wrong),
FN, unclear. Rows with blank/unclear ground_truth are excluded from the
rate calculation and reported separately (a checklist should not treat
an unreviewed sample as "passing").

Usage:
  python compute_accuracy.py --review review_sample.csv
  python compute_accuracy.py --review review_sample.csv --json-out accuracy_report.json
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def load_review(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute(rows: list) -> dict:
    reviewed = [r for r in rows if r.get("ground_truth", "").strip().upper() in ("TP", "FP", "FN")]
    unreviewed_count = len(rows) - len(reviewed)

    tp = sum(1 for r in reviewed if r["ground_truth"].strip().upper() == "TP")
    fp = sum(1 for r in reviewed if r["ground_truth"].strip().upper() == "FP")
    fn = sum(1 for r in reviewed if r["ground_truth"].strip().upper() == "FN")

    total_reviewed = len(reviewed)
    false_positive_rate = (fp / total_reviewed) if total_reviewed else None

    by_camera = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for r in reviewed:
        cam = r.get("camera_id", "unknown")
        gt = r["ground_truth"].strip().upper()
        by_camera[cam][gt.lower()] += 1

    return {
        "total_sampled": len(rows),
        "total_reviewed": total_reviewed,
        "unreviewed_count": unreviewed_count,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "false_positive_rate": false_positive_rate,
        "by_camera": dict(by_camera),
    }


def main():
    parser = argparse.ArgumentParser(description="Compute accuracy stats from a reviewed sample sheet")
    parser.add_argument("--review", required=True, type=str)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args()

    rows = load_review(Path(args.review))
    stats = compute(rows)

    print(f"Reviewed {stats['total_reviewed']} / {stats['total_sampled']} sampled events "
          f"({stats['unreviewed_count']} not yet reviewed)")
    if stats["false_positive_rate"] is not None:
        print(f"False-positive rate: {stats['false_positive_rate']:.1%} "
              f"({stats['false_positives']} FP / {stats['total_reviewed']} reviewed)")
    else:
        print("False-positive rate: N/A — no reviewed rows yet")
    print(f"True positives: {stats['true_positives']}, False negatives: {stats['false_negatives']}")
    print("By camera:")
    for cam, counts in sorted(stats["by_camera"].items()):
        print(f"  {cam}: {counts}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, indent=2))
        print(f"Wrote {args.json_out}")


if __name__ == "__main__":
    main()
