"""Durable current-state store for tasks — the `floorwatch_tasks` table.

Complements (not replaces) the two stores that already exist around
tasks: `floorwatch_event_history` records every lifecycle TRANSITION
(append-only audit trail), while this table holds each task's CURRENT
state, keyed by its unique task_id — the thing you'd JOIN against,
report on, or rehydrate from. Before this, a task's current state lived
only in the leader's process memory (mirrored to a Redis snapshot for
reads): a service restart lost every open task outright. Now the leader
persists every change here and re-loads open tasks on startup.

Clock note (matters for rehydration): TaskRuntime tracks elapsed time on
a MONOTONIC clock, which has no meaning across process restarts. This
store therefore persists wall-clock `started_at`; rehydrate_tasks() maps
it back onto the new process's monotonic clock (elapsed-so-far computed
in wall time, then anchored to the current monotonic reading) — so a
30-minute task that was 10 minutes in when the service restarted resumes
at 10 minutes elapsed, not zero.

Same Postgres-with-JSON-fallback pattern as floorwatch_auth.py /
event_history.py / employee_directory.py.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from floorwatch_logging import get_logger

_log = get_logger("rules-engine.task_store")

_FIELDS = ("task_id", "task_name", "task_type", "zone_id", "camera_id",
           "assigned_minutes", "active_seconds", "status", "workflow_status",
           "assigned_to", "assigned_by", "nudged", "status_nudge_sent",
           "started_at", "updated_at")


def task_runtime_to_record(t, started_at_iso: str) -> dict:
    """TaskRuntime -> plain persistable dict. `started_at_iso` comes from
    the caller (main.py tracks wall-clock start per task) since the
    runtime itself only holds a monotonic anchor."""
    return {
        "task_id": t.task_id, "task_name": t.task_name, "task_type": t.task_type,
        "zone_id": t.zone_id, "camera_id": t.camera_id,
        "assigned_minutes": t.assigned_minutes, "active_seconds": t.active_seconds,
        "status": t.status, "workflow_status": t.workflow_status,
        "assigned_to": t.assigned_to, "assigned_by": t.assigned_by,
        "nudged": t.nudged, "status_nudge_sent": t.status_nudge_sent,
        "started_at": started_at_iso,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def rehydrate_tasks(store, effort_engine, clock) -> int:
    """Loads every OPEN task from the store back into effort_engine.tasks
    after a restart. Returns how many were restored. Completed/flagged/
    resolved tasks stay in the store for reporting but aren't reloaded
    into the live engine."""
    from effort_engine import TaskRuntime

    restored = 0
    now_wall = datetime.now(timezone.utc)
    now_mono = clock()
    for rec in store.list_open():
        try:
            started_at = datetime.fromisoformat(rec["started_at"].replace("Z", "+00:00"))
            elapsed_seconds = max(0.0, (now_wall - started_at).total_seconds())
        except (KeyError, ValueError, AttributeError, TypeError):
            elapsed_seconds = 0.0
        t = TaskRuntime(
            task_id=rec["task_id"], task_name=rec["task_name"], task_type=rec["task_type"],
            zone_id=rec["zone_id"], camera_id=rec.get("camera_id"),
            assigned_minutes=rec["assigned_minutes"],
            start_monotonic=now_mono - elapsed_seconds,
            active_seconds=rec.get("active_seconds", 0.0),
            status=rec.get("status", "open"),
            workflow_status=rec.get("workflow_status", "unassigned"),
            assigned_to=rec.get("assigned_to"), assigned_by=rec.get("assigned_by"),
            nudged=rec.get("nudged", False),
            status_nudge_sent=rec.get("status_nudge_sent", False),
        )
        effort_engine.tasks[t.task_id] = t
        restored += 1
    if restored:
        _log(f"Rehydrated {restored} open task(s) from the durable task store after restart.")
    return restored


class PostgresTaskStore:
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_tasks (
            task_id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            zone_id TEXT NOT NULL,
            camera_id TEXT,
            assigned_minutes DOUBLE PRECISION NOT NULL,
            active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            workflow_status TEXT NOT NULL,
            assigned_to TEXT,
            assigned_by TEXT,
            nudged BOOLEAN NOT NULL DEFAULT false,
            status_nudge_sent BOOLEAN NOT NULL DEFAULT false,
            started_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );
    """
    INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS floorwatch_tasks_status_idx ON floorwatch_tasks (status);",
        "CREATE INDEX IF NOT EXISTS floorwatch_tasks_assignee_idx ON floorwatch_tasks (assigned_to);",
    ]

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL)
            for stmt in self.INDEX_SQL:
                conn.execute(stmt)

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    def upsert(self, record: dict):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_tasks "
                "(task_id, task_name, task_type, zone_id, camera_id, assigned_minutes, "
                " active_seconds, status, workflow_status, assigned_to, assigned_by, "
                " nudged, status_nudge_sent, started_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (task_id) DO UPDATE SET "
                "task_name=EXCLUDED.task_name, assigned_minutes=EXCLUDED.assigned_minutes, "
                "active_seconds=EXCLUDED.active_seconds, status=EXCLUDED.status, "
                "workflow_status=EXCLUDED.workflow_status, assigned_to=EXCLUDED.assigned_to, "
                "assigned_by=EXCLUDED.assigned_by, nudged=EXCLUDED.nudged, "
                "status_nudge_sent=EXCLUDED.status_nudge_sent, updated_at=EXCLUDED.updated_at",
                tuple(record.get(f) for f in _FIELDS),
            )

    def _rows_to_dicts(self, rows) -> list:
        out = []
        for r in rows:
            rec = dict(zip(_FIELDS, r))
            for ts_field in ("started_at", "updated_at"):
                if hasattr(rec[ts_field], "isoformat"):
                    rec[ts_field] = rec[ts_field].isoformat()
            out.append(rec)
        return out

    def list_open(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM floorwatch_tasks WHERE status='open' "
                f"ORDER BY started_at").fetchall()
        return self._rows_to_dicts(rows)

    def get(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM floorwatch_tasks WHERE task_id=%s",
                (task_id,)).fetchone()
        return self._rows_to_dicts([row])[0] if row else None

    def purge_older_than(self, days: int) -> int:
        """Only non-open (finished) tasks are ever purged — an open task
        is live state, not history, regardless of age."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM floorwatch_tasks WHERE status != 'open' "
                "AND updated_at < now() - (%s || ' days')::interval", (days,))
            return cur.rowcount


class JsonTaskStore:
    """Local-file fallback — same caveats as every other JSON fallback in
    this project (doesn't survive a Railway redeploy without a volume)."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def upsert(self, record: dict):
        data = self._load()
        data[record["task_id"]] = record
        self._save(data)

    def list_open(self) -> list:
        return sorted((r for r in self._load().values() if r.get("status") == "open"),
                      key=lambda r: r.get("started_at") or "")

    def get(self, task_id: str) -> Optional[dict]:
        return self._load().get(task_id)

    def purge_older_than(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        data = self._load()
        to_drop = [tid for tid, r in data.items()
                   if r.get("status") != "open" and (r.get("updated_at") or "") < cutoff]
        for tid in to_drop:
            del data[tid]
        if to_drop:
            self._save(data)
        return len(to_drop)


def build_task_store(postgres_dsn: Optional[str], fallback_path: Path):
    if postgres_dsn:
        try:
            store = PostgresTaskStore(postgres_dsn)
            _log(f"Using PostgresTaskStore at {postgres_dsn}")
            return store
        except Exception as e:
            _log(f"could not connect to Postgres for the task store ({e}) — falling back to "
                 f"local JSON. Open tasks will NOT survive a Railway redeploy this way — see "
                 f"RAILWAY_DEPLOYMENT.md.", level="warning")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — task store using the local JSON file "
             "(dev/pilot fallback; open tasks won't survive a redeploy without a volume).")
    return JsonTaskStore(fallback_path)
