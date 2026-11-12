"""Zone directory — lets a deployment's own supervisor/admin add floor
sections ("Concession Counter", "Restroom — Hall A", ...) from the
dashboard instead of editing zones_meta.json and redeploying. Every
zone_id used by the coverage/effort engines only ever needs a
name/role_tag/camera_id label (see engine.py's zones_meta.get(zone_id,
{}).get(...) calls, which already tolerate an unknown zone_id by
falling back to the id itself) — this directory is that label source,
replacing the static zones_meta.json file as the thing operators edit.

Same Postgres-with-JSON-fallback pattern as employee_directory.py — one
more table on the same instance, no new infrastructure. See that
module's docstring for the fallback-durability caveat, which applies
here unchanged.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import floorwatch_db
from floorwatch_logging import get_logger

_log = get_logger("rules-engine.zone_directory")


def validate_zone_id(zone_id: str):
    """Used as a dict key (zones_meta), a Redis/event field, and a URL
    path segment — keep it identifier-safe. Existing zones (e.g.
    "restroomA") mix case, so case is left alone; just bounded and
    restricted to characters safe in all three contexts."""
    s = str(zone_id or "").strip()
    if not s or len(s) > 40:
        return False, "Zone ID must be 1-40 characters."
    if not all(c.isalnum() or c in "-_" for c in s):
        return False, "Zone ID may only contain letters, numbers, hyphens, and underscores."
    return True, None


ACCESS_VALUES = ("open", "closed_hours", "restricted")
ENTRY_POINT_VALUES = ("", "door", "window")


def validate_security(access, entry_point):
    """A zone's two security settings (see security_engine.py). None means "leave as is"."""
    if access is not None and access not in ACCESS_VALUES:
        return False, f"access must be one of: {', '.join(ACCESS_VALUES)}."
    if entry_point is not None and entry_point not in ENTRY_POINT_VALUES:
        return False, "entry_point must be 'door', 'window' or empty."
    return True, None


class PostgresZoneDirectory:
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_zones (
            zone_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role_tag TEXT NOT NULL,
            camera_id TEXT,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by TEXT
        );
    """
    # ADD COLUMN IF NOT EXISTS, not folded into SCHEMA_SQL — this table may
    # already exist (and hold real zones) from before `staffed` existed;
    # CREATE TABLE IF NOT EXISTS alone would silently skip adding it to an
    # already-created table (same pattern as employee_directory.py).
    MIGRATION_SQL = [
        "ALTER TABLE floorwatch_zones ADD COLUMN IF NOT EXISTS staffed BOOLEAN NOT NULL DEFAULT true;",
        # Security alerts (security_engine.py). 'open' / '' = no security rule: nothing changes for existing zones.
        "ALTER TABLE floorwatch_zones ADD COLUMN IF NOT EXISTS access TEXT NOT NULL DEFAULT 'open';",
        "ALTER TABLE floorwatch_zones ADD COLUMN IF NOT EXISTS entry_point TEXT NOT NULL DEFAULT '';",
    ]

    def __init__(self, dsn: str):
        self.dsn = dsn
        # Bumped on every change made through THIS process, so the security engine can notice a zone edit
        # immediately instead of waiting for its periodic refresh (which is what covers other replicas).
        self.version = 0
        with floorwatch_db.connection(dsn) as conn:
            floorwatch_db.apply_schema(conn, "zones", [self.SCHEMA_SQL, *self.MIGRATION_SQL])

    def _connect(self):
        return floorwatch_db.connection(self.dsn)   # pooled — see skills/lib/floorwatch_db.py

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {"zone_id": r[0], "name": r[1], "role_tag": r[2], "camera_id": r[3],
                "active": r[4], "created_at": r[5].isoformat() if r[5] else None, "created_by": r[6],
                "staffed": r[7], "access": r[8], "entry_point": r[9]}

    _COLUMNS = "zone_id, name, role_tag, camera_id, active, created_at, created_by, staffed, access, entry_point"

    def add(self, zone_id: str, name: str, role_tag: str, camera_id: Optional[str] = None,
            created_by: Optional[str] = None, staffed: bool = True,
            access: Optional[str] = None, entry_point: Optional[str] = None):
        self.version += 1
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_zones (zone_id, name, role_tag, camera_id, active, created_by, staffed) "
                "VALUES (%s,%s,%s,%s,true,%s,%s) "
                "ON CONFLICT (zone_id) DO UPDATE SET "
                "name=EXCLUDED.name, role_tag=EXCLUDED.role_tag, camera_id=EXCLUDED.camera_id",
                (zone_id, name, role_tag, camera_id or None, created_by, staffed),
            )
        if access is not None or entry_point is not None:
            self.set_security(zone_id, access, entry_point)

    def get(self, zone_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_zones WHERE zone_id=%s", (zone_id,)  # nosec B608 - only a constant column list / fixed clause text is interpolated; every value is a bound %s parameter
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self, active_only: bool = False) -> list:
        where = "WHERE active = true" if active_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_zones {where} ORDER BY zone_id",  # nosec B608 - only a constant column list / fixed clause text is interpolated; every value is a bound %s parameter
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_active(self, zone_id: str, active: bool) -> bool:
        self.version += 1
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_zones SET active=%s WHERE zone_id=%s", (active, zone_id))
            return cur.rowcount > 0

    def set_staffed(self, zone_id: str, staffed: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_zones SET staffed=%s WHERE zone_id=%s", (staffed, zone_id))
            return cur.rowcount > 0

    def set_security(self, zone_id: str, access: Optional[str] = None, entry_point: Optional[str] = None) -> bool:
        ok, reason = validate_security(access, entry_point)
        if not ok:
            raise ValueError(reason)
        self.version += 1
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_zones SET access=COALESCE(%s, access), entry_point=COALESCE(%s, entry_point) "
                "WHERE zone_id=%s", (access, entry_point, zone_id))
            return cur.rowcount > 0


class JsonZoneDirectory:
    """Local-file fallback when FLOORWATCH_POSTGRES_DSN isn't set — same
    honest-fallback pattern as JsonEmployeeDirectory. Does NOT survive a
    Railway redeploy without a mounted volume."""

    def __init__(self, path: Path):
        self.path = path
        self.version = 0     # see PostgresZoneDirectory.__init__

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict):
        # Write-then-rename: the security engine re-reads this file from a background thread, and a plain
        # write_text() can be seen half-written (which _load would read as "no zones at all").
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:          # Windows: a reader has the file open at this instant
                if attempt == 4:
                    raise
                time.sleep(0.02)

    def add(self, zone_id: str, name: str, role_tag: str, camera_id: Optional[str] = None,
            created_by: Optional[str] = None, staffed: bool = True,
            access: Optional[str] = None, entry_point: Optional[str] = None):
        from datetime import datetime, timezone
        ok, reason = validate_security(access, entry_point)
        if not ok:
            raise ValueError(reason)
        self.version += 1
        data = self._load()
        existing = data.get(zone_id, {})
        data[zone_id] = {
            "zone_id": zone_id, "name": name, "role_tag": role_tag, "camera_id": camera_id or None,
            "active": existing.get("active", True),
            "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "created_by": existing.get("created_by") or created_by,
            "staffed": existing.get("staffed", staffed),
            "access": access if access is not None else existing.get("access", "open"),
            "entry_point": entry_point if entry_point is not None else existing.get("entry_point", ""),
        }
        self._save(data)

    @staticmethod
    def _with_defaults(entry: dict) -> dict:
        # Records written before the security settings existed simply lack the keys.
        entry.setdefault("access", "open")
        entry.setdefault("entry_point", "")
        return entry

    def get(self, zone_id: str) -> Optional[dict]:
        entry = self._load().get(zone_id)
        return self._with_defaults(entry) if entry else None

    def list_all(self, active_only: bool = False) -> list:
        entries = sorted((self._with_defaults(e) for e in self._load().values()), key=lambda e: e["zone_id"])
        if active_only:
            entries = [e for e in entries if e.get("active", True)]
        return entries

    def set_active(self, zone_id: str, active: bool) -> bool:
        self.version += 1
        data = self._load()
        if zone_id not in data:
            return False
        data[zone_id]["active"] = active
        self._save(data)
        return True

    def set_staffed(self, zone_id: str, staffed: bool) -> bool:
        data = self._load()
        if zone_id not in data:
            return False
        data[zone_id]["staffed"] = staffed
        self._save(data)
        return True

    def set_security(self, zone_id: str, access: Optional[str] = None, entry_point: Optional[str] = None) -> bool:
        ok, reason = validate_security(access, entry_point)
        if not ok:
            raise ValueError(reason)
        self.version += 1
        data = self._load()
        if zone_id not in data:
            return False
        if access is not None:
            data[zone_id]["access"] = access
        if entry_point is not None:
            data[zone_id]["entry_point"] = entry_point
        self._save(data)
        return True


def build_zone_directory(postgres_dsn: Optional[str], fallback_path: Path):
    """Same honest-fallback dispatch as build_employee_directory()."""
    if postgres_dsn:
        try:
            directory = PostgresZoneDirectory(postgres_dsn)
            _log(f"Using PostgresZoneDirectory at {floorwatch_db.safe_target(postgres_dsn)}")
            return directory
        except Exception as e:
            _log(f"could not connect to Postgres for zone directory ({e}) — falling back "
                 f"to local JSON. This will NOT survive a Railway redeploy without a mounted "
                 f"volume — see RAILWAY_DEPLOYMENT.md.", level="warning")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — zone directory using the local JSON "
             "file (dev/pilot fallback; won't survive a Railway redeploy without a volume).")
    return JsonZoneDirectory(fallback_path)
