"""Unit tests for the shared JSONL retention/pruning helper."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from floorwatch_retention import parse_timestamp, prune_jsonl_file  # noqa: E402

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _write_jsonl(path: Path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries))


def test_parse_timestamp_valid():
    ts = parse_timestamp("2026-07-24T10:00:00Z")
    assert ts.year == 2026 and ts.month == 7 and ts.day == 24


def test_parse_timestamp_invalid_returns_none():
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-timestamp") is None


def test_prune_missing_file_returns_zeros(tmp_path):
    result = prune_jsonl_file(tmp_path / "nonexistent.jsonl", retention_days=90, now=NOW)
    assert result == {"kept": 0, "pruned": 0, "undated": 0}


def test_prune_keeps_recent_entries(tmp_path):
    p = tmp_path / "digest.jsonl"
    _write_jsonl(p, [{"timestamp": "2026-07-28T10:00:00Z", "event_type": "zone_gap"}])
    result = prune_jsonl_file(p, retention_days=90, now=NOW)
    assert result == {"kept": 1, "pruned": 0, "undated": 0}
    assert len(p.read_text().splitlines()) == 1


def test_prune_removes_old_entries(tmp_path):
    p = tmp_path / "digest.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    _write_jsonl(p, [{"timestamp": old_ts, "event_type": "zone_gap"}])
    result = prune_jsonl_file(p, retention_days=90, now=NOW)
    assert result == {"kept": 0, "pruned": 1, "undated": 0}
    assert p.read_text() == ""


def test_prune_keeps_undated_entries_rather_than_guessing(tmp_path):
    p = tmp_path / "digest.jsonl"
    _write_jsonl(p, [{"event_type": "zone_gap"}])  # no timestamp field
    result = prune_jsonl_file(p, retention_days=90, now=NOW)
    assert result == {"kept": 1, "pruned": 0, "undated": 1}


def test_prune_keeps_malformed_lines_rather_than_dropping_data(tmp_path):
    p = tmp_path / "digest.jsonl"
    p.write_text("not valid json at all")
    result = prune_jsonl_file(p, retention_days=90, now=NOW)
    assert result == {"kept": 1, "pruned": 0, "undated": 0}
    assert "not valid json" in p.read_text()


def test_prune_mixed_old_and_new(tmp_path):
    p = tmp_path / "digest.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    new_ts = (NOW - timedelta(days=1)).isoformat()
    _write_jsonl(p, [
        {"timestamp": old_ts, "id": "old"},
        {"timestamp": new_ts, "id": "new"},
    ])
    result = prune_jsonl_file(p, retention_days=90, now=NOW)
    assert result == {"kept": 1, "pruned": 1, "undated": 0}
    remaining = [json.loads(line) for line in p.read_text().splitlines()]
    assert remaining == [{"timestamp": new_ts, "id": "new"}]


def test_dry_run_reports_but_does_not_modify_file(tmp_path):
    p = tmp_path / "digest.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    original_content = json.dumps({"timestamp": old_ts, "id": "old"})
    p.write_text(original_content)
    result = prune_jsonl_file(p, retention_days=90, dry_run=True, now=NOW)
    assert result == {"kept": 0, "pruned": 1, "undated": 0}
    assert p.read_text() == original_content  # untouched


def test_archive_writes_pruned_entries_before_deletion(tmp_path):
    p = tmp_path / "digest.jsonl"
    archive_dir = tmp_path / "archive"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    _write_jsonl(p, [{"timestamp": old_ts, "id": "old"}])

    prune_jsonl_file(p, retention_days=90, archive_dir=archive_dir, now=NOW)

    archive_files = list(archive_dir.glob("*.jsonl"))
    assert len(archive_files) == 1
    archived = json.loads(archive_files[0].read_text().strip())
    assert archived["id"] == "old"
    assert p.read_text() == ""  # removed from the live file


def test_archive_prefix_is_customizable(tmp_path):
    p = tmp_path / "notes.jsonl"
    archive_dir = tmp_path / "archive"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    _write_jsonl(p, [{"timestamp": old_ts, "text": "old note"}])

    prune_jsonl_file(p, retention_days=90, archive_dir=archive_dir, now=NOW,
                      archive_prefix="incident_notes_archived")

    archive_files = list(archive_dir.glob("incident_notes_archived_*.jsonl"))
    assert len(archive_files) == 1
