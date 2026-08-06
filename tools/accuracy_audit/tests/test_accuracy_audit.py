"""Unit tests for the accuracy-audit sampling harness and stats computation."""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_events import hour_bucket, load_events, stratify, sample, write_csv  # noqa: E402
from compute_accuracy import compute  # noqa: E402


def test_hour_bucket_boundaries():
    assert hour_bucket("2026-07-24T02:00:00Z") == "night"
    assert hour_bucket("2026-07-24T08:00:00Z") == "morning"
    assert hour_bucket("2026-07-24T14:00:00Z") == "afternoon"
    assert hour_bucket("2026-07-24T19:00:00Z") == "evening"
    assert hour_bucket("2026-07-24T22:00:00Z") == "late_night"


def test_hour_bucket_malformed_timestamp():
    assert hour_bucket("") == "unknown"
    assert hour_bucket("not-a-timestamp") == "unknown"


def _write_digest(path: Path, events: list):
    path.write_text("\n".join(json.dumps(e) for e in events))


def test_load_events_from_multiple_files(tmp_path):
    f1 = tmp_path / "d1.jsonl"
    f2 = tmp_path / "d2.jsonl"
    _write_digest(f1, [{"event_id": "1"}])
    _write_digest(f2, [{"event_id": "2"}, {"event_id": "3"}])
    events = load_events([str(f1), str(f2)])
    assert len(events) == 3


def test_load_events_skips_missing_file(tmp_path):
    events = load_events([str(tmp_path / "nonexistent.jsonl")])
    assert events == []


def test_stratify_groups_by_camera_and_hour_bucket():
    events = [
        {"camera_id": "cam1", "timestamp": "2026-07-24T08:00:00Z"},
        {"camera_id": "cam1", "timestamp": "2026-07-24T09:00:00Z"},  # same stratum
        {"camera_id": "cam2", "timestamp": "2026-07-24T20:00:00Z"},
    ]
    strata = stratify(events)
    assert len(strata) == 2
    assert len(strata[("cam1", "morning")]) == 2
    assert len(strata[("cam2", "evening")]) == 1


def test_sample_respects_per_stratum_cap():
    events = [{"camera_id": "cam1", "timestamp": "2026-07-24T08:00:00Z", "event_id": str(i)} for i in range(10)]
    strata = stratify(events)
    rows = sample(strata, per_stratum=3, seed=1)
    assert len(rows) == 3
    for row in rows:
        assert row["ground_truth"] == ""  # left blank for reviewer


def test_sample_is_reproducible_with_same_seed():
    events = [{"camera_id": "cam1", "timestamp": "2026-07-24T08:00:00Z", "event_id": str(i)} for i in range(10)]
    strata = stratify(events)
    rows1 = sample(strata, per_stratum=3, seed=7)
    rows2 = sample(strata, per_stratum=3, seed=7)
    assert [r["event_id"] for r in rows1] == [r["event_id"] for r in rows2]


def test_write_csv_roundtrip(tmp_path):
    rows = [{
        "camera_id": "cam1", "hour_bucket": "morning", "event_id": "e1",
        "timestamp": "2026-07-24T08:00:00Z", "zone_id": "z1", "event_type": "zone_gap",
        "confidence": 0.9, "message": "", "ground_truth": "", "reviewer_notes": "",
    }]
    out = tmp_path / "review.csv"
    write_csv(rows, out)
    with open(out, newline="") as f:
        read_rows = list(csv.DictReader(f))
    assert read_rows[0]["event_id"] == "e1"


# ── compute_accuracy ──────────────────────────────────────────────────────

def test_compute_accuracy_basic_rates():
    rows = [
        {"ground_truth": "TP", "camera_id": "cam1"},
        {"ground_truth": "FP", "camera_id": "cam1"},
        {"ground_truth": "FP", "camera_id": "cam2"},
        {"ground_truth": "TP", "camera_id": "cam2"},
    ]
    stats = compute(rows)
    assert stats["total_reviewed"] == 4
    assert stats["false_positives"] == 2
    assert stats["false_positive_rate"] == 0.5
    assert stats["unreviewed_count"] == 0


def test_compute_accuracy_excludes_unreviewed_rows():
    rows = [
        {"ground_truth": "TP", "camera_id": "cam1"},
        {"ground_truth": "", "camera_id": "cam1"},
        {"ground_truth": "unclear", "camera_id": "cam1"},
    ]
    stats = compute(rows)
    assert stats["total_reviewed"] == 1
    assert stats["unreviewed_count"] == 2
    assert stats["false_positive_rate"] == 0.0


def test_compute_accuracy_no_reviewed_rows_returns_none_rate():
    rows = [{"ground_truth": "", "camera_id": "cam1"}]
    stats = compute(rows)
    assert stats["false_positive_rate"] is None
