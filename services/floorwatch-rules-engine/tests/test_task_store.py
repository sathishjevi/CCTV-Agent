"""Unit tests for task_store.py — durable current-state store for tasks,
including the monotonic-clock rehydration math (a restarted service must
resume a task at its real elapsed time, not zero)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from effort_engine import EffortEngine, TaskRuntime  # noqa: E402
from task_store import (  # noqa: E402
    JsonTaskStore, PostgresTaskStore, build_task_store, rehydrate_tasks,
    task_runtime_to_record,
)

NOW = datetime.now(timezone.utc)


def make_runtime(task_id="t1", status="open", workflow_status="notified", active_seconds=120.0):
    return TaskRuntime(
        task_id=task_id, task_name="Clean Door", task_type="clean_door", zone_id="theatre3",
        camera_id="cam3", assigned_minutes=30.0, start_monotonic=1000.0,
        active_seconds=active_seconds, status=status, workflow_status=workflow_status,
        assigned_to="101", assigned_by="user:admin",
    )


# ── serialization ───────────────────────────────────────────────────────

def test_task_runtime_to_record_captures_both_dimensions():
    rec = task_runtime_to_record(make_runtime(), NOW.isoformat())
    assert rec["task_id"] == "t1"
    assert rec["status"] == "open"
    assert rec["workflow_status"] == "notified"
    assert rec["assigned_to"] == "101"
    assert rec["started_at"] == NOW.isoformat()
    assert rec["updated_at"]  # stamped at serialization time


# ── JsonTaskStore ───────────────────────────────────────────────────────

def test_json_upsert_and_get_roundtrips(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    store.upsert(task_runtime_to_record(make_runtime(), NOW.isoformat()))
    rec = store.get("t1")
    assert rec["task_name"] == "Clean Door"
    assert rec["workflow_status"] == "notified"


def test_json_upsert_overwrites_same_task_id(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    store.upsert(task_runtime_to_record(make_runtime(active_seconds=10), NOW.isoformat()))
    store.upsert(task_runtime_to_record(make_runtime(active_seconds=99), NOW.isoformat()))
    assert store.get("t1")["active_seconds"] == 99


def test_json_list_open_excludes_finished(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    store.upsert(task_runtime_to_record(make_runtime("t1", status="open"), NOW.isoformat()))
    store.upsert(task_runtime_to_record(make_runtime("t2", status="resolved"), NOW.isoformat()))
    store.upsert(task_runtime_to_record(make_runtime("t3", status="flagged"), NOW.isoformat()))
    assert [r["task_id"] for r in store.list_open()] == ["t1"]


def test_json_purge_only_touches_finished_tasks(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    old_iso = (NOW - timedelta(days=60)).isoformat()

    ancient_open = task_runtime_to_record(make_runtime("open_old", status="open"), old_iso)
    ancient_open["updated_at"] = old_iso
    ancient_done = task_runtime_to_record(make_runtime("done_old", status="resolved"), old_iso)
    ancient_done["updated_at"] = old_iso
    fresh_done = task_runtime_to_record(make_runtime("done_new", status="resolved"), NOW.isoformat())

    for rec in (ancient_open, ancient_done, fresh_done):
        store.upsert(rec)

    purged = store.purge_older_than(30)
    assert purged == 1
    assert store.get("open_old") is not None   # open = live state, never purged
    assert store.get("done_old") is None
    assert store.get("done_new") is not None


# ── rehydration ─────────────────────────────────────────────────────────

class AllStaffedRoster:
    def is_zone_staffed(self, zone_id):
        return True


class NullDigest:
    def append(self, evt):
        pass


def make_engine(clock):
    return EffortEngine(
        roster=AllStaffedRoster(), digest=NullDigest(),
        zones_meta={"theatre3": {"name": "Theatre 3", "role_tag": "janitorial", "camera_id": "cam3"}},
        task_type_thresholds={"_default": {"expected_active_ratio": 0.5}},
        clock=clock,
    )


def test_rehydrate_restores_open_tasks_with_real_elapsed_time(tmp_path):
    # Captured fresh here, not from the module-level NOW — this test may
    # run a couple minutes after collection in a full suite run, and
    # rehydrate_tasks() compares against a FRESH datetime.now() internally,
    # so the "10 minutes ago" anchor must be relative to right now too.
    ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    store = JsonTaskStore(tmp_path / "tasks.json")
    store.upsert(task_runtime_to_record(make_runtime(active_seconds=300), ten_min_ago))

    clock = lambda: 5000.0  # noqa: E731 — the "new process's" monotonic clock
    engine = make_engine(clock)
    restored = rehydrate_tasks(store, engine, clock)

    assert restored == 1
    t = engine.tasks["t1"]
    elapsed_seconds = clock() - t.start_monotonic
    assert 9.5 * 60 <= elapsed_seconds <= 10.5 * 60  # ~10 real minutes, not zero
    assert t.active_seconds == 300
    assert t.workflow_status == "notified"
    assert t.assigned_to == "101"


def test_rehydrate_skips_finished_tasks(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    store.upsert(task_runtime_to_record(make_runtime("done", status="resolved"), NOW.isoformat()))
    engine = make_engine(lambda: 5000.0)
    assert rehydrate_tasks(store, engine, lambda: 5000.0) == 0
    assert engine.tasks == {}


def test_rehydrate_bad_started_at_defaults_to_zero_elapsed(tmp_path):
    store = JsonTaskStore(tmp_path / "tasks.json")
    rec = task_runtime_to_record(make_runtime(), NOW.isoformat())
    rec["started_at"] = "not-a-timestamp"
    store.upsert(rec)
    clock = lambda: 5000.0  # noqa: E731
    engine = make_engine(clock)
    rehydrate_tasks(store, engine, clock)
    assert engine.tasks["t1"].start_monotonic == 5000.0  # zero elapsed, not a crash


# ── PostgresTaskStore (mocked — no real Postgres in this sandbox) ────────

def _fake_psycopg_module(fake_conn):
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value.__enter__ = MagicMock(return_value=fake_conn)
    fake_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
    return fake_psycopg


def test_postgres_schema_created_on_init():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresTaskStore("postgresql://fake/dsn")
    all_sql = " ".join(c.args[0] for c in fake_conn.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS floorwatch_tasks" in all_sql


def test_postgres_upsert_uses_on_conflict_update():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresTaskStore("postgresql://fake/dsn")
        store.upsert(task_runtime_to_record(make_runtime(), NOW.isoformat()))
    call_args = fake_conn.execute.call_args_list[-1]
    assert "ON CONFLICT (task_id) DO UPDATE" in call_args[0][0]
    assert call_args[0][1][0] == "t1"


def test_postgres_purge_excludes_open_tasks():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.rowcount = 2
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        store = PostgresTaskStore("postgresql://fake/dsn")
        assert store.purge_older_than(30) == 2
    call_args = fake_conn.execute.call_args_list[-1]
    assert "status != 'open'" in call_args[0][0]


# ── build dispatch ──────────────────────────────────────────────────────

def test_build_uses_json_fallback_when_no_dsn(tmp_path):
    assert isinstance(build_task_store(None, tmp_path / "tasks.json"), JsonTaskStore)


def test_build_falls_back_on_postgres_failure(tmp_path):
    with patch("task_store.PostgresTaskStore", side_effect=Exception("refused")):
        store = build_task_store("postgresql://unreachable/dsn", tmp_path / "tasks.json")
    assert isinstance(store, JsonTaskStore)
