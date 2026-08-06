"""Unit tests for the shift-digest summarization job."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shift_digest_job import event_date, load_digest, main, summarize, to_markdown  # noqa: E402


def _evt(event_type, zone_id, timestamp):
    return {"event_type": event_type, "zone_id": zone_id, "timestamp": timestamp,
            "camera_id": "cam1", "confidence": 0.9}


def test_event_date_extracts_calendar_date():
    assert event_date("2026-07-24T18:00:00Z") == "2026-07-24"


def test_event_date_handles_malformed_timestamp():
    assert event_date("garbage") == "unknown"
    assert event_date("") == "unknown"


def test_load_digest_missing_file_returns_empty(tmp_path):
    assert load_digest(tmp_path / "nonexistent.jsonl") == []


def test_load_digest_skips_malformed_lines(tmp_path):
    path = tmp_path / "digest.jsonl"
    path.write_text('{"event_type": "zone_gap"}\nnot json\n{"event_type": "zone_escalated"}')
    events = load_digest(path)
    assert len(events) == 2


# ── summarize: recurring vs one-off tagging ──────────────────────────────

def test_summarize_tags_one_off_when_only_seen_today():
    events = [_evt("zone_escalated", "concession", "2026-07-24T08:00:00Z")]
    summary = summarize(events, "2026-07-24")
    assert summary["patterns"][0]["pattern"] == "one_off"
    assert summary["one_off_pattern_count"] == 1
    assert summary["recurring_pattern_count"] == 0


def test_summarize_tags_recurring_when_seen_on_multiple_days_same_zone_and_bucket():
    events = [
        _evt("zone_gap", "concession", "2026-07-20T08:00:00Z"),   # morning, day 1
        _evt("zone_gap", "concession", "2026-07-22T08:30:00Z"),   # morning, day 2
        _evt("zone_escalated", "concession", "2026-07-24T08:15:00Z"),  # morning, target day
    ]
    summary = summarize(events, "2026-07-24")
    assert len(summary["patterns"]) == 1
    p = summary["patterns"][0]
    assert p["pattern"] == "recurring"
    assert p["distinct_dates_seen"] == 3
    assert p["event_count_today"] == 1


def test_summarize_different_hour_buckets_are_separate_patterns():
    events = [
        _evt("zone_gap", "concession", "2026-07-24T08:00:00Z"),   # morning
        _evt("zone_gap", "concession", "2026-07-24T20:00:00Z"),   # evening
    ]
    summary = summarize(events, "2026-07-24")
    assert len(summary["patterns"]) == 2
    buckets = {p["hour_bucket"] for p in summary["patterns"]}
    assert buckets == {"morning", "evening"}


def test_summarize_excludes_events_from_other_days():
    events = [
        _evt("zone_gap", "concession", "2026-07-23T08:00:00Z"),
        _evt("zone_gap", "concession", "2026-07-24T08:00:00Z"),
    ]
    summary = summarize(events, "2026-07-24")
    assert summary["total_events_today"] == 1


def test_summarize_by_event_type_and_zone_counts():
    events = [
        _evt("zone_gap", "concession", "2026-07-24T08:00:00Z"),
        _evt("zone_gap", "lobby", "2026-07-24T08:00:00Z"),
        _evt("task_flag", "concession", "2026-07-24T09:00:00Z"),
    ]
    summary = summarize(events, "2026-07-24")
    assert summary["by_event_type"] == {"zone_gap": 2, "task_flag": 1}
    assert summary["by_zone"] == {"concession": 2, "lobby": 1}


def test_summarize_empty_digest_produces_empty_summary():
    summary = summarize([], "2026-07-24")
    assert summary["total_events_today"] == 0
    assert summary["patterns"] == []


# ── markdown rendering ────────────────────────────────────────────────

def test_to_markdown_includes_date_and_counts():
    summary = summarize([_evt("zone_gap", "concession", "2026-07-24T08:00:00Z")], "2026-07-24")
    md = to_markdown(summary)
    assert "2026-07-24" in md
    assert "zone_gap" in md
    assert "concession" in md


# ── CLI end-to-end ────────────────────────────────────────────────────

def test_cli_writes_json_and_markdown_outputs(tmp_path, capsys, monkeypatch):
    digest_path = tmp_path / "digest.jsonl"
    digest_path.write_text("\n".join(json.dumps(e) for e in [
        _evt("zone_escalated", "concession", "2026-07-20T08:00:00Z"),
        _evt("zone_escalated", "concession", "2026-07-24T08:10:00Z"),
    ]))
    out_dir = tmp_path / "out"

    monkeypatch.setattr(sys, "argv", [
        "shift_digest_job.py", "--digest", str(digest_path),
        "--date", "2026-07-24", "--out-dir", str(out_dir),
    ])
    main()

    json_path = out_dir / "2026-07-24.json"
    md_path = out_dir / "2026-07-24.md"
    assert json_path.exists()
    assert md_path.exists()

    summary = json.loads(json_path.read_text())
    assert summary["patterns"][0]["pattern"] == "recurring"

    captured = capsys.readouterr()
    assert "recurring" in captured.out.lower()
