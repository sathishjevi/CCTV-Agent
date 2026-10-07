"""Register of data-subject requests (access, correction, erasure, ...).

Privacy laws give people rights over their data — and a deadline for honouring
them (GDPR: 30 days; CCPA/CPRA: 45; PIPEDA: 30). A right nobody can find a
request for, or that nobody notices is overdue, isn't a right in practice. This
is the register: each request gets a due date when it's logged, shows up
overdue if it slips, and can only be closed with an outcome and the name of
whoever closed it. Overdue requests raise an error-level alert (see
main.py's retention/privacy loop) so someone is told, not just a row turning
red.

Same Postgres-with-local-file-fallback shape as the other stores."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import floorwatch_db
from floorwatch_logging import get_logger

_log = get_logger("rules-engine.privacy_requests")

KINDS = ("access", "portability", "correction", "erasure", "restriction", "objection", "other")
OUTCOMES = ("fulfilled", "partially_fulfilled", "declined")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _present(record: dict, now: Optional[datetime] = None) -> dict:
    """Adds `overdue`, computed fresh each time rather than stored (it changes with the clock)."""
    now = now or _now()
    out = dict(record)
    due = datetime.fromisoformat(out["due_at"])
    out["overdue"] = out["status"] == "open" and due < now
    return out


class PostgresPrivacyRequests:
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_privacy_requests (
            request_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            details TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            due_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            resolved_by TEXT,
            outcome TEXT,
            resolution_note TEXT
        );
    """
    INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS floorwatch_privacy_requests_status_idx "
        "ON floorwatch_privacy_requests (status, due_at);",
        "CREATE INDEX IF NOT EXISTS floorwatch_privacy_requests_subject_idx "
        "ON floorwatch_privacy_requests (subject_id);",
    ]
    _COLUMNS = ("request_id, subject_id, kind, details, status, created_at, due_at, resolved_at, "
                "resolved_by, outcome, resolution_note")

    def __init__(self, dsn: str):
        self.dsn = dsn
        with floorwatch_db.connection(dsn) as conn:
            floorwatch_db.apply_schema(conn, "privacy_requests", [self.SCHEMA_SQL, *self.INDEX_SQL])

    def _connect(self):
        return floorwatch_db.connection(self.dsn)

    @staticmethod
    def _row(r) -> dict:
        iso = lambda v: v.isoformat() if v else None  # noqa: E731
        return {"request_id": r[0], "subject_id": r[1], "kind": r[2], "details": r[3], "status": r[4],
                "created_at": iso(r[5]), "due_at": iso(r[6]), "resolved_at": iso(r[7]),
                "resolved_by": r[8], "outcome": r[9], "resolution_note": r[10]}

    def create(self, subject_id: str, kind: str, details: Optional[str], due_days: int) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        request_id = uuid.uuid4().hex[:16]
        with self._connect() as conn:
            row = conn.execute(
                f"INSERT INTO floorwatch_privacy_requests (request_id, subject_id, kind, details, due_at) "  # nosec B608 - constant column list; values are bound parameters
                f"VALUES (%s, %s, %s, %s, now() + (%s || ' days')::interval) RETURNING {self._COLUMNS}",
                (request_id, subject_id, kind, details, due_days)).fetchone()
        return _present(self._row(row))

    def get(self, request_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_privacy_requests WHERE request_id=%s",  # nosec B608 - constant column list; values are bound parameters
                (request_id,)).fetchone()
        return _present(self._row(row)) if row else None

    def list(self, status: Optional[str] = None, subject_id: Optional[str] = None) -> list:
        clauses, params = [], []
        if status:
            clauses.append("status = %s")
            params.append(status)
        if subject_id:
            clauses.append("subject_id = %s")
            params.append(subject_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_privacy_requests {where} ORDER BY due_at",  # nosec B608 - constant column list / fixed clause text; values are bound parameters
                tuple(params)).fetchall()
        return [_present(self._row(r)) for r in rows]

    def resolve(self, request_id: str, resolved_by: str, outcome: str, note: Optional[str]) -> bool:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_privacy_requests SET status='resolved', resolved_at=now(), "
                "resolved_by=%s, outcome=%s, resolution_note=%s WHERE request_id=%s AND status='open'",
                (resolved_by, outcome, note, request_id))
            return cur.rowcount > 0

    def purge_resolved_older_than(self, days: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM floorwatch_privacy_requests WHERE status='resolved' "
                "AND resolved_at < now() - (%s || ' days')::interval", (days,))
            return cur.rowcount


class JsonPrivacyRequests:
    """Local-file fallback — same honest caveat as the other JSON stores: it
    doesn't survive a Railway redeploy without a volume."""

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

    def create(self, subject_id: str, kind: str, details: Optional[str], due_days: int) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        now = _now()
        record = {"request_id": uuid.uuid4().hex[:16], "subject_id": subject_id, "kind": kind, "details": details,
                  "status": "open", "created_at": now.isoformat(), "due_at": (now + timedelta(days=due_days)).isoformat(),
                  "resolved_at": None, "resolved_by": None, "outcome": None, "resolution_note": None}
        data = self._load()
        data[record["request_id"]] = record
        self._save(data)
        return _present(record)

    def get(self, request_id: str) -> Optional[dict]:
        record = self._load().get(request_id)
        return _present(record) if record else None

    def list(self, status: Optional[str] = None, subject_id: Optional[str] = None) -> list:
        records = sorted(self._load().values(), key=lambda r: r["due_at"])
        if status:
            records = [r for r in records if r["status"] == status]
        if subject_id:
            records = [r for r in records if r["subject_id"] == subject_id]
        return [_present(r) for r in records]

    def resolve(self, request_id: str, resolved_by: str, outcome: str, note: Optional[str]) -> bool:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        data = self._load()
        record = data.get(request_id)
        if record is None or record["status"] != "open":
            return False
        record.update({"status": "resolved", "resolved_at": _now().isoformat(), "resolved_by": resolved_by,
                       "outcome": outcome, "resolution_note": note})
        self._save(data)
        return True

    def purge_resolved_older_than(self, days: int) -> int:
        cutoff = (_now() - timedelta(days=days)).isoformat()
        data = self._load()
        drop = [k for k, r in data.items() if r["status"] == "resolved" and (r["resolved_at"] or "") < cutoff]
        for k in drop:
            del data[k]
        if drop:
            self._save(data)
        return len(drop)


def build_privacy_requests(postgres_dsn: Optional[str], fallback_path: Path):
    if postgres_dsn:
        try:
            store = PostgresPrivacyRequests(postgres_dsn)
            _log(f"Using PostgresPrivacyRequests at {floorwatch_db.safe_target(postgres_dsn)}")
            return store
        except Exception as e:
            _log(f"could not connect to Postgres for privacy requests ({e}) — falling back to local JSON.",
                 level="warning")
    return JsonPrivacyRequests(fallback_path)
