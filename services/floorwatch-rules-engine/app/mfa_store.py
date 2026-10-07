"""Storage for two-step-verification (TOTP) enrolment.

One record per login identity, keyed by "dashboard:<username>" or
"employee:<employee number>" (those are separate namespaces — see
revocation_subject()). A record holds the authenticator secret ENCRYPTED
(floorwatch_crypto), the one-time recovery codes as keyed hashes (never the
codes themselves), and the last time-step a code was accepted so a code can't
be used twice.

The secret is written when enrolment STARTS but only counts once the person
proves their authenticator works by entering a code (`enabled`). Re-starting
enrolment before then simply replaces it."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import floorwatch_db
from floorwatch_logging import get_logger

_log = get_logger("rules-engine.mfa_store")


class PostgresMfaStore:
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_mfa (
            subject TEXT PRIMARY KEY,
            secret_enc TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT false,
            recovery_hashes TEXT NOT NULL DEFAULT '[]',
            last_step BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            enabled_at TIMESTAMPTZ
        );
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        with floorwatch_db.connection(dsn) as conn:
            floorwatch_db.apply_schema(conn, "mfa", [self.SCHEMA_SQL])

    def _connect(self):
        return floorwatch_db.connection(self.dsn)

    def get(self, subject: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secret_enc, enabled, recovery_hashes, last_step FROM floorwatch_mfa WHERE subject=%s",
                (subject,)).fetchone()
        if row is None:
            return None
        return {"secret_enc": row[0], "enabled": row[1], "recovery_hashes": json.loads(row[2]), "last_step": row[3]}

    def begin_enrollment(self, subject: str, secret_enc: str) -> bool:
        """False if already enabled (disable it first). Replaces an unfinished enrolment."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO floorwatch_mfa (subject, secret_enc, enabled, recovery_hashes, last_step) "
                "VALUES (%s, %s, false, '[]', 0) ON CONFLICT (subject) DO UPDATE SET "
                "secret_enc=EXCLUDED.secret_enc, last_step=0, created_at=now() WHERE floorwatch_mfa.enabled = false",
                (subject, secret_enc))
            return cur.rowcount > 0

    def enable(self, subject: str, recovery_hashes: List[str], last_step: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_mfa SET enabled=true, enabled_at=now(), recovery_hashes=%s, last_step=%s "
                "WHERE subject=%s AND enabled=false", (json.dumps(recovery_hashes), last_step, subject))
            return cur.rowcount > 0

    def disable(self, subject: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM floorwatch_mfa WHERE subject=%s", (subject,)).rowcount > 0

    def accept_step(self, subject: str, step: int) -> bool:
        """Atomically records that this time-step's code was used. False if it (or a later one) already was —
        which is how a replayed code, or two simultaneous logins with the same code, lose the race."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE floorwatch_mfa SET last_step=%s WHERE subject=%s AND last_step < %s",
                (step, subject, step)).rowcount > 0

    def consume_recovery(self, subject: str, code_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recovery_hashes FROM floorwatch_mfa WHERE subject=%s AND enabled", (subject,)).fetchone()
            if row is None:
                return False
            hashes = json.loads(row[0])
            if code_hash not in hashes:
                return False
            remaining = [h for h in hashes if h != code_hash]
            # compare-and-set on the old list so two simultaneous uses of one code can't both succeed
            return conn.execute(
                "UPDATE floorwatch_mfa SET recovery_hashes=%s WHERE subject=%s AND recovery_hashes=%s",
                (json.dumps(remaining), subject, row[0])).rowcount > 0

    def replace_recovery(self, subject: str, recovery_hashes: List[str]) -> bool:
        with self._connect() as conn:
            return conn.execute("UPDATE floorwatch_mfa SET recovery_hashes=%s WHERE subject=%s AND enabled",
                                (json.dumps(recovery_hashes), subject)).rowcount > 0

    def enabled_subjects(self) -> set:
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT subject FROM floorwatch_mfa WHERE enabled").fetchall()}


class JsonMfaStore:
    """Local-file fallback — same caveat as the other JSON stores (lost on a redeploy without a volume)."""

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

    def get(self, subject: str) -> Optional[dict]:
        r = self._load().get(subject)
        return None if r is None else {k: r[k] for k in ("secret_enc", "enabled", "recovery_hashes", "last_step")}

    def begin_enrollment(self, subject: str, secret_enc: str) -> bool:
        data = self._load()
        if data.get(subject, {}).get("enabled"):
            return False
        data[subject] = {"secret_enc": secret_enc, "enabled": False, "recovery_hashes": [], "last_step": 0,
                         "created_at": datetime.now(timezone.utc).isoformat(), "enabled_at": None}
        self._save(data)
        return True

    def enable(self, subject: str, recovery_hashes: List[str], last_step: int) -> bool:
        data = self._load()
        r = data.get(subject)
        if r is None or r["enabled"]:
            return False
        r.update({"enabled": True, "recovery_hashes": recovery_hashes, "last_step": last_step,
                  "enabled_at": datetime.now(timezone.utc).isoformat()})
        self._save(data)
        return True

    def disable(self, subject: str) -> bool:
        data = self._load()
        if subject not in data:
            return False
        del data[subject]
        self._save(data)
        return True

    def accept_step(self, subject: str, step: int) -> bool:
        data = self._load()
        r = data.get(subject)
        if r is None or r["last_step"] >= step:
            return False
        r["last_step"] = step
        self._save(data)
        return True

    def consume_recovery(self, subject: str, code_hash: str) -> bool:
        data = self._load()
        r = data.get(subject)
        if r is None or not r["enabled"] or code_hash not in r["recovery_hashes"]:
            return False
        r["recovery_hashes"] = [h for h in r["recovery_hashes"] if h != code_hash]
        self._save(data)
        return True

    def replace_recovery(self, subject: str, recovery_hashes: List[str]) -> bool:
        data = self._load()
        r = data.get(subject)
        if r is None or not r["enabled"]:
            return False
        r["recovery_hashes"] = recovery_hashes
        self._save(data)
        return True

    def enabled_subjects(self) -> set:
        return {k for k, r in self._load().items() if r["enabled"]}


def build_mfa_store(postgres_dsn: Optional[str], fallback_path: Path):
    if postgres_dsn:
        try:
            store = PostgresMfaStore(postgres_dsn)
            _log(f"Using PostgresMfaStore at {floorwatch_db.safe_target(postgres_dsn)}")
            return store
        except Exception as e:
            _log(f"could not connect to Postgres for the MFA store ({e}) — falling back to local JSON.", level="warning")
    return JsonMfaStore(fallback_path)
