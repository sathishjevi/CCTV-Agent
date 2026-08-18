"""Durable event-history store — every zone/task lifecycle event
(assignment, nudges, flags, resolutions, supervisor actions), not just
the narrow zone_escalated/task_flag pair digest_store.py has always
captured.

Why this exists: engine.py/effort_engine.py's state (zones/tasks) lives
only in the current leader's process memory, mirrored into Redis as a
SNAPSHOT (see cluster_bus.py) — current state only, overwritten on every
change, gone on restart. The WebSocket broadcast is fire-and-forget —
seen live by connected dashboards, never stored anywhere. Before this
module, the only durable record of ANYTHING was digest_store.py, and
even that skipped task_assigned and task_resolved (including a
supervisor's confirm/dismiss) entirely — a real gap, reported directly:
a supervisor reviewed and confirmed a flagged task, watched it happen
live in the dashboard, and it was never written to any database.

Same interface, same honest-fallback dispatch as UserStore/
PostgresUserStore in floorwatch_auth.py — build_event_history_store()
tries Postgres (reusing the same instance accounts use — one more
table, no second database) and falls back to a local JSONL file if
unconfigured/unreachable.

Deliberately excludes task_active_time_update (a ~30s heartbeat ping per
open task) — see main.py's _emit() for where that filter is applied.
Recording every heartbeat forever would bloat storage for no audit
value; the final resolution event already carries the final
active-minutes figure.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from floorwatch_logging import get_logger
from floorwatch_retention import prune_jsonl_file

_log = get_logger("rules-engine.event_history")


class PostgresEventHistoryStore:
    """Real Postgres implementation — see floorwatch_auth.py's
    PostgresUserStore docstring for why this reuses whatever Postgres
    instance is already configured rather than standing up a second
    database. `data` stores the full event dict as JSONB (nothing lost,
    no schema to keep in sync with the event shape) with a few columns
    broken out for indexed filtering."""

    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_event_history (
            event_id TEXT PRIMARY KEY,
            event_timestamp TIMESTAMPTZ NOT NULL,
            event_type TEXT NOT NULL,
            zone_id TEXT,
            task_id TEXT,
            data JSONB NOT NULL
        );
    """
    INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS floorwatch_event_history_ts_idx "
        "ON floorwatch_event_history (event_timestamp);",
        "CREATE INDEX IF NOT EXISTS floorwatch_event_history_type_idx "
        "ON floorwatch_event_history (event_type);",
        "CREATE INDEX IF NOT EXISTS floorwatch_event_history_zone_idx "
        "ON floorwatch_event_history (zone_id);",
        "CREATE INDEX IF NOT EXISTS floorwatch_event_history_task_idx "
        "ON floorwatch_event_history (task_id);",
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

    def record(self, event: dict):
        event_id = event.get("event_id")
        if not event_id:
            return  # no stable key to dedupe on — skip rather than guess one
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_event_history "
                "(event_id, event_timestamp, event_type, zone_id, task_id, data) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (event_id) DO NOTHING",
                (event_id, event.get("timestamp"), event.get("event_type"),
                 event.get("zone_id"), event.get("task_id"), json.dumps(event)),
            )

    def query(self, event_type: Optional[str] = None, zone_id: Optional[str] = None,
              task_id: Optional[str] = None, since: Optional[str] = None,
              until: Optional[str] = None, limit: int = 500) -> list:
        clauses, params = [], []
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if zone_id:
            clauses.append("zone_id = %s")
            params.append(zone_id)
        if task_id:
            clauses.append("task_id = %s")
            params.append(task_id)
        if since:
            clauses.append("event_timestamp >= %s")
            params.append(since)
        if until:
            clauses.append("event_timestamp <= %s")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT data FROM floorwatch_event_history {where} "
                f"ORDER BY event_timestamp DESC LIMIT %s",
                (*params, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def purge_older_than(self, days: int, archive_dir: Optional[Path] = None,
                          archive_prefix: str = "event_history_archived",
                          dry_run: bool = False, now: Optional[datetime] = None) -> dict:
        """Deletes rows older than `days`. If `archive_dir` is given (and
        dry_run is False), purged rows are appended to a dated JSONL file
        there BEFORE deletion — archived, not silently destroyed,
        matching retention.py's existing digest/account purging. Returns
        {"purged": N, "kept": M}."""
        now = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM floorwatch_event_history "
                "WHERE event_timestamp < now() - (%s || ' days')::interval",
                (days,),
            ).fetchall()
            kept = conn.execute(
                "SELECT COUNT(*) FROM floorwatch_event_history "
                "WHERE event_timestamp >= now() - (%s || ' days')::interval",
                (days,),
            ).fetchone()[0]

            if dry_run:
                return {"purged": len(rows), "kept": kept}

            if rows and archive_dir is not None:
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = archive_dir / f"{archive_prefix}_{now:%Y%m%d}.jsonl"
                with open(archive_path, "a", encoding="utf-8") as f:
                    for (data,) in rows:
                        f.write(json.dumps(data) + "\n")

            conn.execute(
                "DELETE FROM floorwatch_event_history "
                "WHERE event_timestamp < now() - (%s || ' days')::interval",
                (days,),
            )
            return {"purged": len(rows), "kept": kept}


class JsonlEventHistoryStore:
    """Local-file fallback when FLOORWATCH_POSTGRES_DSN isn't set — same
    honest-fallback pattern as every other store in this project. Does
    NOT survive a Railway redeploy without a mounted volume; use
    Postgres for anything beyond local dev/testing."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def query(self, event_type: Optional[str] = None, zone_id: Optional[str] = None,
              task_id: Optional[str] = None, since: Optional[str] = None,
              until: Optional[str] = None, limit: int = 500) -> list:
        if not self.path.exists():
            return []
        results = []
        # newest first, matching PostgresEventHistoryStore's ORDER BY DESC
        for line in reversed(self.path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type and evt.get("event_type") != event_type:
                continue
            if zone_id and evt.get("zone_id") != zone_id:
                continue
            if task_id and evt.get("task_id") != task_id:
                continue
            ts = evt.get("timestamp")
            # ISO-8601 strings from _now_iso() sort lexicographically the
            # same as chronologically — plain string comparison is safe
            # here without a full datetime parse.
            if since and (ts is None or ts < since):
                continue
            if until and (ts is None or ts > until):
                continue
            results.append(evt)
            if len(results) >= limit:
                break
        return results

    def purge_older_than(self, days: int, archive_dir: Optional[Path] = None,
                          archive_prefix: str = "event_history_archived",
                          dry_run: bool = False) -> dict:
        result = prune_jsonl_file(self.path, days, archive_dir=archive_dir, dry_run=dry_run,
                                   timestamp_field="timestamp", archive_prefix=archive_prefix)
        return {"purged": result["pruned"], "kept": result["kept"]}


def build_event_history_store(postgres_dsn: Optional[str], fallback_path: Path):
    """Same honest-fallback dispatch as build_user_store()/
    build_vector_store() elsewhere in this project."""
    if postgres_dsn:
        try:
            store = PostgresEventHistoryStore(postgres_dsn)
            _log(f"Using PostgresEventHistoryStore at {postgres_dsn}")
            return store
        except Exception as e:
            _log(f"could not connect to Postgres for event history ({e}) — falling back to "
                 f"local JSONL. This will NOT survive a Railway redeploy without a mounted "
                 f"volume — see RAILWAY_DEPLOYMENT.md.", level="warning")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — event history using the local JSONL "
             "file (dev/pilot fallback; won't survive a Railway redeploy without a volume).")
    return JsonlEventHistoryStore(fallback_path)
