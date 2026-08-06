"""End-to-end CLI tests for retention.py — actually running main(), not
just the shared prune_jsonl_file() helper (already unit-tested in
skills/lib/test_floorwatch_retention.py)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import retention  # noqa: E402

NOW = datetime.now(timezone.utc)


def test_retention_main_prunes_old_digest_entries(tmp_path, monkeypatch, capsys):
    digest_path = tmp_path / "shift_digest.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    new_ts = (NOW - timedelta(days=1)).isoformat()
    digest_path.write_text("\n".join(json.dumps(e) for e in [
        {"timestamp": old_ts, "event_type": "zone_escalated"},
        {"timestamp": new_ts, "event_type": "zone_gap"},
    ]))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", digest_path)
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--no-archive"])

    retention.main()

    out = capsys.readouterr().out
    assert "Pruned 1 entries" in out
    remaining = [json.loads(line) for line in digest_path.read_text().splitlines()]
    assert len(remaining) == 1
    assert remaining[0]["event_type"] == "zone_gap"


def test_retention_main_dry_run_does_not_modify_file(tmp_path, monkeypatch, capsys):
    digest_path = tmp_path / "shift_digest.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    original = json.dumps({"timestamp": old_ts, "event_type": "zone_escalated"})
    digest_path.write_text(original)

    monkeypatch.setattr(retention.config, "DIGEST_PATH", digest_path)
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--dry-run"])

    retention.main()

    out = capsys.readouterr().out
    assert "Would prune 1" in out
    assert digest_path.read_text() == original


def test_retention_main_archives_before_deleting(tmp_path, monkeypatch, capsys):
    digest_path = tmp_path / "shift_digest.jsonl"
    archive_dir = tmp_path / "my_archive"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    digest_path.write_text(json.dumps({"timestamp": old_ts, "event_type": "zone_escalated"}))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", digest_path)
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--archive-dir", str(archive_dir)])

    retention.main()

    assert list(archive_dir.glob("shift_digest_archived_*.jsonl"))


def test_retention_main_respects_cli_override(tmp_path, monkeypatch, capsys):
    digest_path = tmp_path / "shift_digest.jsonl"
    ts_45_days_ago = (NOW - timedelta(days=45)).isoformat()
    digest_path.write_text(json.dumps({"timestamp": ts_45_days_ago, "event_type": "zone_gap"}))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", digest_path)
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)  # default would keep this entry
    monkeypatch.setattr(sys, "argv", ["retention.py", "--retention-days", "30", "--no-archive"])

    retention.main()

    assert digest_path.read_text() == ""  # pruned because --retention-days 30 overrode the 90-day default
