"""End-to-end CLI tests for retention.py — actually running main(), not
just the shared prune_jsonl_file() helper (already unit-tested in
skills/lib/test_floorwatch_retention.py)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import retention  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def isolate_user_store(tmp_path, monkeypatch):
    """Every test below drives retention.main(), which (DP-M4) now also
    purges stale accounts and event_history — point both at isolated,
    empty local stores so no test ever reads/writes a real local file on
    the machine running these tests."""
    monkeypatch.setattr(retention.config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(retention.config, "POSTGRES_DSN", "")


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


# ── DP-M4: account purging, wired through the same CLI ─────────────────

def test_retention_main_purges_stale_deactivated_accounts(tmp_path, monkeypatch, capsys):
    from floorwatch_auth import UserStore

    users_path = tmp_path / "users.json"
    store = UserStore(users_path)
    store.create_user("still_active", "pw1234567890", role="viewer")
    store.create_user("long_gone", "pw1234567890", role="viewer")
    store.set_active("long_gone", False)
    raw = json.loads(users_path.read_text())
    raw["long_gone"]["deactivated_at"] = (NOW - timedelta(days=200)).isoformat()
    users_path.write_text(json.dumps(raw))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "USERS_PATH", users_path)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--no-archive"])

    retention.main()

    out = capsys.readouterr().out
    assert "Pruned 1 accounts" in out
    remaining = json.loads(users_path.read_text())
    assert "still_active" in remaining
    assert "long_gone" not in remaining


def test_retention_main_account_purge_dry_run_does_not_modify_store(tmp_path, monkeypatch, capsys):
    from floorwatch_auth import UserStore

    users_path = tmp_path / "users.json"
    store = UserStore(users_path)
    store.create_user("long_gone", "pw1234567890", role="viewer")
    store.set_active("long_gone", False)
    raw = json.loads(users_path.read_text())
    raw["long_gone"]["deactivated_at"] = (NOW - timedelta(days=200)).isoformat()
    users_path.write_text(json.dumps(raw))
    before = users_path.read_text()

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "USERS_PATH", users_path)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--dry-run"])

    retention.main()

    out = capsys.readouterr().out
    assert "Would prune 1 accounts" in out
    assert users_path.read_text() == before


def test_retention_main_account_purge_archives_before_deleting(tmp_path, monkeypatch, capsys):
    from floorwatch_auth import UserStore

    users_path = tmp_path / "users.json"
    archive_dir = tmp_path / "my_archive"
    store = UserStore(users_path)
    store.create_user("long_gone", "pw1234567890", role="viewer")
    store.set_active("long_gone", False)
    raw = json.loads(users_path.read_text())
    raw["long_gone"]["deactivated_at"] = (NOW - timedelta(days=200)).isoformat()
    users_path.write_text(json.dumps(raw))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "USERS_PATH", users_path)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--archive-dir", str(archive_dir)])

    retention.main()

    archived = list(archive_dir.glob("accounts_archived_*.jsonl"))
    assert archived
    assert "long_gone" in archived[0].read_text()


def test_retention_main_never_purges_active_accounts(tmp_path, monkeypatch, capsys):
    from floorwatch_auth import UserStore

    users_path = tmp_path / "users.json"
    store = UserStore(users_path)
    store.create_user("very_old_but_active", "pw1234567890", role="viewer",
                       created_by=None)
    # backdate created_at only — account has never been deactivated
    raw = json.loads(users_path.read_text())
    raw["very_old_but_active"]["created_at"] = (NOW - timedelta(days=1000)).isoformat()
    users_path.write_text(json.dumps(raw))

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "USERS_PATH", users_path)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--no-archive"])

    retention.main()

    out = capsys.readouterr().out
    assert "Pruned 0 accounts" in out
    assert "very_old_but_active" in json.loads(users_path.read_text())


# ── event_history purging ────────────────────────────────────────────────

def test_retention_main_prunes_old_event_history_entries(tmp_path, monkeypatch, capsys):
    events_path = tmp_path / "event_history.jsonl"
    old_evt = {"event_id": "old", "event_type": "task_assigned",
               "timestamp": (NOW - timedelta(days=50)).isoformat()}
    recent_evt = {"event_id": "recent", "event_type": "task_assigned",
                  "timestamp": (NOW - timedelta(days=1)).isoformat()}
    events_path.write_text(json.dumps(old_evt) + "\n" + json.dumps(recent_evt) + "\n")

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_PATH", events_path)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_RETENTION_DAYS", 30)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--no-archive"])

    retention.main()

    out = capsys.readouterr().out
    assert "Pruned 1 event_history entries" in out
    remaining = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [e["event_id"] for e in remaining] == ["recent"]


def test_retention_main_respects_event_history_retention_days_cli_override(tmp_path, monkeypatch, capsys):
    events_path = tmp_path / "event_history.jsonl"
    evt = {"event_id": "e1", "event_type": "task_assigned",
           "timestamp": (NOW - timedelta(days=20)).isoformat()}
    events_path.write_text(json.dumps(evt) + "\n")

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_PATH", events_path)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_RETENTION_DAYS", 30)  # default would keep this
    monkeypatch.setattr(sys, "argv", ["retention.py", "--event-history-retention-days", "14", "--no-archive"])

    retention.main()

    assert events_path.read_text() == ""  # pruned because 14-day override beat the 30-day default


def test_retention_main_event_history_purge_dry_run_does_not_modify_file(tmp_path, monkeypatch, capsys):
    events_path = tmp_path / "event_history.jsonl"
    evt = {"event_id": "old", "event_type": "task_assigned",
           "timestamp": (NOW - timedelta(days=50)).isoformat()}
    events_path.write_text(json.dumps(evt) + "\n")
    before = events_path.read_text()

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_PATH", events_path)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_RETENTION_DAYS", 30)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--dry-run"])

    retention.main()

    out = capsys.readouterr().out
    assert "Would prune 1 event_history entries" in out
    assert events_path.read_text() == before


def test_retention_main_event_history_purge_archives_before_deleting(tmp_path, monkeypatch, capsys):
    events_path = tmp_path / "event_history.jsonl"
    archive_dir = tmp_path / "my_archive"
    evt = {"event_id": "old", "event_type": "task_assigned",
           "timestamp": (NOW - timedelta(days=50)).isoformat()}
    events_path.write_text(json.dumps(evt) + "\n")

    monkeypatch.setattr(retention.config, "DIGEST_PATH", tmp_path / "shift_digest.jsonl")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_PATH", events_path)
    monkeypatch.setattr(retention.config, "EVENT_HISTORY_RETENTION_DAYS", 30)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--archive-dir", str(archive_dir)])

    retention.main()

    archived = list(archive_dir.glob("event_history_archived_*.jsonl"))
    assert archived
    assert "old" in archived[0].read_text()
