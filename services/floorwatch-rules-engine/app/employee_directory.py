"""Employee/supervisor directory — the assignee side of the task
workflow (who a task can be assigned to, and how to reach them).

Distinct from floorwatch_users (floorwatch_auth.py): that table is LOGIN
ACCOUNTS for people who operate this dashboard (admins/supervisors/
viewers). This table is the FLOOR STAFF a task can be assigned to —
most of whom never log into anything; they interact purely over SMS.
A supervisor can exist in both (a login account to run the dashboard,
and a directory entry so tasks can be assigned to them) — the optional
`account_username` column links the two when that's the case, which is
also the hook for eventually scoping which department a logged-in
supervisor may assign within.

Phone numbers are PII — never logged unmasked (callers use
notifications.py's _mask_phone for any log line), stored only here.

Same Postgres-with-JSONL-fallback pattern as floorwatch_auth.py /
event_history.py — one more table on the same instance, no new
infrastructure.
"""

import json
from pathlib import Path
from typing import Optional

from floorwatch_logging import get_logger

_log = get_logger("rules-engine.employee_directory")

DIRECTORY_ROLES = {"employee", "supervisor"}
NOTIFY_CHANNELS = {"sms", "fcm"}


def validate_channel(channel: Optional[str]):
    """None/"" is valid — means "no per-employee override, fall back to
    the deployment's global NOTIFY_CHANNEL default" (see main.py's
    _resolve_channel()). Anything non-empty must be one of NOTIFY_CHANNELS."""
    if not channel:
        return True, None
    if channel not in NOTIFY_CHANNELS:
        return False, f"channel must be one of {sorted(NOTIFY_CHANNELS)} (or omitted)"
    return True, None


def validate_primary_contact(role: str, is_primary_contact: bool):
    """The primary contact for a department is always a supervisor —
    never a line employee (see employee_directory.py's module docstring
    and PHASE_2_NOTES.md's auto-assignment section). Rejecting this at
    write time, rather than silently allowing it, is what keeps
    auto-assignment's Global Constraint 2 reasoning ("only a supervisor's
    identity is ever auto-used") actually true in practice, not just in
    the design doc."""
    if is_primary_contact and role != "supervisor":
        return False, "is_primary_contact can only be set on a 'supervisor' record"
    return True, None


def validate_employee_number(employee_number: str):
    """Employee numbers are client-issued identifiers (e.g. "101") — keep
    the rules loose enough for real-world formats (digits, letters,
    hyphens) but bounded, and safe to embed in SMS text and URLs."""
    s = str(employee_number or "").strip()
    if not s or len(s) > 20:
        return False, "Employee number must be 1-20 characters."
    if not all(c.isalnum() or c == "-" for c in s):
        return False, "Employee number may only contain letters, numbers, and hyphens."
    return True, None


def validate_phone(phone: str):
    """E.164-leaning check, permissive enough for local formats: optional
    leading +, then 7-15 digits (spaces/hyphens tolerated and stripped
    by normalize_phone below). Deliberately NOT a strict E.164 parser —
    real client rosters have messy numbers, and Twilio does its own
    validation at send time anyway; this just rejects obvious garbage."""
    s = normalize_phone(phone)
    if not s:
        return False, "Phone number is required."
    digits = s[1:] if s.startswith("+") else s
    if not digits.isdigit() or not (7 <= len(digits) <= 15):
        return False, "Phone number must be 7-15 digits, optionally starting with +."
    return True, None


def normalize_phone(phone: str) -> str:
    """Strips spaces/hyphens/parens so the same number always compares
    equal — critical for the inbound-SMS webhook, which must map a
    sender's number back to a directory entry reliably."""
    return "".join(c for c in str(phone or "") if c.isdigit() or c == "+")


class PostgresEmployeeDirectory:
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_employees (
            employee_number TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT NOT NULL,
            phone TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT true,
            account_username TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by TEXT
        );
    """
    # ADD COLUMN IF NOT EXISTS, not folded into SCHEMA_SQL above — this
    # table may already exist (and hold real rows) from before Feature
    # 1/2 shipped; CREATE TABLE IF NOT EXISTS alone would silently skip
    # adding these columns to an already-created table.
    MIGRATION_SQL = [
        "ALTER TABLE floorwatch_employees ADD COLUMN IF NOT EXISTS channel TEXT;",
        "ALTER TABLE floorwatch_employees ADD COLUMN IF NOT EXISTS fcm_token TEXT;",
        "ALTER TABLE floorwatch_employees ADD COLUMN IF NOT EXISTS "
        "is_primary_contact BOOLEAN NOT NULL DEFAULT false;",
    ]
    INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS floorwatch_employees_dept_idx "
        "ON floorwatch_employees (department);",
        "CREATE INDEX IF NOT EXISTS floorwatch_employees_phone_idx "
        "ON floorwatch_employees (phone);",
    ]

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL)
            for stmt in self.MIGRATION_SQL:
                conn.execute(stmt)
            for stmt in self.INDEX_SQL:
                conn.execute(stmt)

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {"employee_number": r[0], "name": r[1], "role": r[2], "department": r[3],
                "phone": r[4], "active": r[5], "account_username": r[6],
                "created_at": r[7].isoformat() if r[7] else None, "created_by": r[8],
                "channel": r[9], "fcm_token": r[10], "is_primary_contact": r[11]}

    _COLUMNS = ("employee_number, name, role, department, phone, active, "
                "account_username, created_at, created_by, channel, fcm_token, is_primary_contact")

    def add(self, employee_number: str, name: str, role: str, department: str, phone: str,
            account_username: Optional[str] = None, created_by: Optional[str] = None,
            channel: Optional[str] = None, fcm_token: Optional[str] = None,
            is_primary_contact: bool = False):
        if role not in DIRECTORY_ROLES:
            raise ValueError(f"invalid directory role: {role!r}")
        ok, reason = validate_channel(channel)
        if not ok:
            raise ValueError(reason)
        ok, reason = validate_primary_contact(role, is_primary_contact)
        if not ok:
            raise ValueError(reason)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_employees "
                "(employee_number, name, role, department, phone, active, account_username, "
                "created_by, channel, fcm_token, is_primary_contact) "
                "VALUES (%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s) "
                "ON CONFLICT (employee_number) DO UPDATE SET "
                "name=EXCLUDED.name, role=EXCLUDED.role, department=EXCLUDED.department, "
                "phone=EXCLUDED.phone, account_username=EXCLUDED.account_username, "
                "channel=EXCLUDED.channel, fcm_token=EXCLUDED.fcm_token, "
                "is_primary_contact=EXCLUDED.is_primary_contact",
                (employee_number, name, role, department, normalize_phone(phone),
                 account_username, created_by, channel or None, fcm_token or None,
                 is_primary_contact),
            )

    def get(self, employee_number: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_employees WHERE employee_number=%s",
                (employee_number,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_phone(self, phone: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_employees WHERE phone=%s AND active=true",
                (normalize_phone(phone),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self, department: Optional[str] = None, active_only: bool = False) -> list:
        clauses, params = [], []
        if department:
            clauses.append("department = %s")
            params.append(department)
        if active_only:
            clauses.append("active = true")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_employees {where} ORDER BY employee_number",
                tuple(params),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_active(self, employee_number: str, active: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_employees SET active=%s WHERE employee_number=%s",
                (active, employee_number))
            return cur.rowcount > 0


class JsonEmployeeDirectory:
    """Local-file fallback when FLOORWATCH_POSTGRES_DSN isn't set — same
    honest-fallback pattern as UserStore. Does NOT survive a Railway
    redeploy without a mounted volume."""

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

    def add(self, employee_number: str, name: str, role: str, department: str, phone: str,
            account_username: Optional[str] = None, created_by: Optional[str] = None,
            channel: Optional[str] = None, fcm_token: Optional[str] = None,
            is_primary_contact: bool = False):
        if role not in DIRECTORY_ROLES:
            raise ValueError(f"invalid directory role: {role!r}")
        ok, reason = validate_channel(channel)
        if not ok:
            raise ValueError(reason)
        ok, reason = validate_primary_contact(role, is_primary_contact)
        if not ok:
            raise ValueError(reason)
        from datetime import datetime, timezone
        data = self._load()
        existing = data.get(employee_number, {})
        data[employee_number] = {
            "employee_number": employee_number, "name": name, "role": role,
            "department": department, "phone": normalize_phone(phone),
            "active": existing.get("active", True),
            "account_username": account_username,
            "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "created_by": existing.get("created_by") or created_by,
            "channel": channel or None, "fcm_token": fcm_token or None,
            "is_primary_contact": is_primary_contact,
        }
        self._save(data)

    def get(self, employee_number: str) -> Optional[dict]:
        return self._load().get(employee_number)

    def get_by_phone(self, phone: str) -> Optional[dict]:
        normalized = normalize_phone(phone)
        for entry in self._load().values():
            if entry.get("phone") == normalized and entry.get("active", True):
                return entry
        return None

    def list_all(self, department: Optional[str] = None, active_only: bool = False) -> list:
        entries = sorted(self._load().values(), key=lambda e: e["employee_number"])
        if department:
            entries = [e for e in entries if e.get("department") == department]
        if active_only:
            entries = [e for e in entries if e.get("active", True)]
        return entries

    def set_active(self, employee_number: str, active: bool) -> bool:
        data = self._load()
        if employee_number not in data:
            return False
        data[employee_number]["active"] = active
        self._save(data)
        return True


def build_employee_directory(postgres_dsn: Optional[str], fallback_path: Path):
    """Same honest-fallback dispatch as build_user_store()/
    build_event_history_store()."""
    if postgres_dsn:
        try:
            directory = PostgresEmployeeDirectory(postgres_dsn)
            _log(f"Using PostgresEmployeeDirectory at {postgres_dsn}")
            return directory
        except Exception as e:
            _log(f"could not connect to Postgres for employee directory ({e}) — falling back "
                 f"to local JSON. This will NOT survive a Railway redeploy without a mounted "
                 f"volume — see RAILWAY_DEPLOYMENT.md.", level="warning")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — employee directory using the local JSON "
             "file (dev/pilot fallback; won't survive a Railway redeploy without a volume).")
    return JsonEmployeeDirectory(fallback_path)
