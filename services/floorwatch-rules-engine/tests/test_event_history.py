"""Unit tests for event_history.py — the durable audit trail of the full
zone/task lifecycle (assignment, nudges, flags, resolutions, supervisor
actions), reported missing directly: a supervisor confirmed a flagged
task, watched it happen live in the dashboard, and it was never recorded
anywhere durable. See event_history.py's module docstring."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from event_history import (  # noqa: E402
    JsonlEventHistoryStore, PostgresEventHistoryStore, build_event_history_store,
)

NOW = datetime.now(timezone.utc)


def _evt(event_id, event_type, zone_id=None, task_id=None, ts=None):
    return {
        "event_id": event_id, "event_type": event_type, "zone_id": zone_id, "task_id": task_id,
        "timestamp": ts or NOW.isoformat(), "message": f"{event_type} for testing",
    }


# ── JsonlEventHistoryStore ──────────────────────────────────────────────

def test_jsonl_record_then_query_roundtrips(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("e1", "task_assigned", task_id="t1"))
    results = store.query()
    assert len(results) == 1
    assert results[0]["event_id"] == "e1"


def test_jsonl_query_returns_newest_first(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("e1", "task_assigned", ts=(NOW - timedelta(minutes=2)).isoformat()))
    store.record(_evt("e2", "task_resolved", ts=(NOW - timedelta(minutes=1)).isoformat()))
    results = store.query()
    assert [r["event_id"] for r in results] == ["e2", "e1"]


def test_jsonl_query_filters_by_event_type(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("e1", "task_assigned"))
    store.record(_evt("e2", "task_flag"))
    results = store.query(event_type="task_flag")
    assert [r["event_id"] for r in results] == ["e2"]


def test_jsonl_query_filters_by_zone_id(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("e1", "zone_gap", zone_id="lobby"))
    store.record(_evt("e2", "zone_gap", zone_id="boxoffice"))
    results = store.query(zone_id="lobby")
    assert [r["event_id"] for r in results] == ["e1"]


def test_jsonl_query_filters_by_task_id(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("e1", "task_assigned", task_id="t1"))
    store.record(_evt("e2", "task_assigned", task_id="t2"))
    results = store.query(task_id="t2")
    assert [r["event_id"] for r in results] == ["e2"]


def test_jsonl_query_filters_by_time_range(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("old", "task_assigned", ts=(NOW - timedelta(days=10)).isoformat()))
    store.record(_evt("recent", "task_assigned", ts=NOW.isoformat()))
    results = store.query(since=(NOW - timedelta(days=1)).isoformat())
    assert [r["event_id"] for r in results] == ["recent"]


def test_jsonl_query_respects_limit(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    for i in range(5):
        store.record(_evt(f"e{i}", "task_assigned", ts=(NOW - timedelta(minutes=i)).isoformat()))
    results = store.query(limit=2)
    assert len(results) == 2


def test_jsonl_query_on_missing_file_returns_empty(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "does_not_exist.jsonl")
    assert store.query() == []


def test_jsonl_purge_older_than_removes_old_entries(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("old", "task_assigned", ts=(NOW - timedelta(days=40)).isoformat()))
    store.record(_evt("recent", "task_assigned", ts=(NOW - timedelta(days=1)).isoformat()))
    result = store.purge_older_than(30)
    assert result == {"purged": 1, "kept": 1}
    remaining = store.query()
    assert [r["event_id"] for r in remaining] == ["recent"]


def test_jsonl_purge_dry_run_changes_nothing(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("old", "task_assigned", ts=(NOW - timedelta(days=40)).isoformat()))
    result = store.purge_older_than(30, dry_run=True)
    assert result == {"purged": 1, "kept": 0}
    assert len(store.query()) == 1  # nothing actually removed


def test_jsonl_purge_archives_before_deleting(tmp_path):
    store = JsonlEventHistoryStore(tmp_path / "event_history.jsonl")
    store.record(_evt("old", "task_assigned", ts=(NOW - timedelta(days=40)).isoformat()))
    archive_dir = tmp_path / "archive"
    store.purge_older_than(30, archive_dir=archive_dir)
    archived = list(archive_dir.glob("event_history_archived_*.jsonl"))
    assert archived
    assert "old" in archived[0].read_text()


# ── PostgresEventHistoryStore (mocked — no real Postgres in this sandbox) ──

def _fake_psycopg_module(fake_conn):
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value.__enter__ = MagicMock(return_value=fake_conn)
    fake_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
    return fake_psycopg


def test_postgres_schema_and_indexes_created_on_init():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresEventHistoryStore("postgresql://fake/dsn")

    all_sql = " ".join(call.args[0] for call in fake_conn.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS floorwatch_event_history" in all_sql
    assert "CREATE INDEX" in all_sql


def test_postgres_record_runs_insert_with_conflict_handling():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresEventHistoryStore("postgresql://fake/dsn")
        store.record(_evt("e1", "task_assigned", zone_id="lobby", task_id="t1"))

    call_args = fake_conn.execute.call_args_list[-1]
    assert "INSERT INTO floorwatch_event_history" in call_args[0][0]
    assert "ON CONFLICT (event_id) DO NOTHING" in call_args[0][0]
    assert call_args[0][1][0] == "e1"


def test_postgres_record_skips_event_with_no_event_id():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresEventHistoryStore("postgresql://fake/dsn")
        calls_before = len(fake_conn.execute.call_args_list)
        store.record({"event_type": "task_assigned"})  # no event_id
        assert len(fake_conn.execute.call_args_list) == calls_before


def test_postgres_query_builds_where_clause_from_filters():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [({"event_id": "e1"},)]
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresEventHistoryStore("postgresql://fake/dsn")
        result = store.query(event_type="task_flag", zone_id="lobby")

    assert result == [{"event_id": "e1"}]
    call_args = fake_conn.execute.call_args_list[-1]
    assert "event_type = %s" in call_args[0][0]
    assert "zone_id = %s" in call_args[0][0]


def test_postgres_purge_dry_run_does_not_delete():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [({"event_id": "old"},)]
    fake_conn.execute.return_value.fetchone.return_value = (3,)
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresEventHistoryStore("postgresql://fake/dsn")
        result = store.purge_older_than(30, dry_run=True)

    assert result == {"purged": 1, "kept": 3}
    # the DELETE statement itself must never have been issued
    delete_calls = [c for c in fake_conn.execute.call_args_list if "DELETE" in c.args[0]]
    assert delete_calls == []


def test_postgres_purge_archives_before_deleting(tmp_path):
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [({"event_id": "old", "event_type": "task_assigned"},)]
    fake_conn.execute.return_value.fetchone.return_value = (0,)
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresEventHistoryStore("postgresql://fake/dsn")
        archive_dir = tmp_path / "archive"
        result = store.purge_older_than(30, archive_dir=archive_dir)

    assert result == {"purged": 1, "kept": 0}
    archived = list(archive_dir.glob("event_history_archived_*.jsonl"))
    assert archived
    assert "old" in archived[0].read_text()
    delete_calls = [c for c in fake_conn.execute.call_args_list if "DELETE" in c.args[0]]
    assert len(delete_calls) == 1


# ── build_event_history_store dispatch ──────────────────────────────────

def test_build_event_history_store_uses_jsonl_fallback_when_no_dsn(tmp_path):
    store = build_event_history_store(None, tmp_path / "event_history.jsonl")
    assert isinstance(store, JsonlEventHistoryStore)


def test_build_event_history_store_falls_back_on_postgres_connection_failure(tmp_path):
    with patch("event_history.PostgresEventHistoryStore", side_effect=Exception("connection refused")):
        store = build_event_history_store("postgresql://unreachable/dsn", tmp_path / "event_history.jsonl")
    assert isinstance(store, JsonlEventHistoryStore)


def test_build_event_history_store_uses_postgres_when_reachable(tmp_path):
    fake_store = MagicMock()
    with patch("event_history.PostgresEventHistoryStore", return_value=fake_store):
        store = build_event_history_store("postgresql://fake/dsn", tmp_path / "event_history.jsonl")
    assert store is fake_store
