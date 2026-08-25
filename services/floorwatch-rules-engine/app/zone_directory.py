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
from pathlib import Path
from typing import Optional

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
    ]

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL)
            for stmt in self.MIGRATION_SQL:
                conn.execute(stmt)

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {"zone_id": r[0], "name": r[1], "role_tag": r[2], "camera_id": r[3],
                "active": r[4], "created_at": r[5].isoformat() if r[5] else None, "created_by": r[6],
                "staffed": r[7]}

    _COLUMNS = "zone_id, name, role_tag, camera_id, active, created_at, created_by, staffed"

    def add(self, zone_id: str, name: str, role_tag: str, camera_id: Optional[str] = None,
            created_by: Optional[str] = None, staffed: bool = True):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_zones (zone_id, name, role_tag, camera_id, active, created_by, staffed) "
                "VALUES (%s,%s,%s,%s,true,%s,%s) "
                "ON CONFLICT (zone_id) DO UPDATE SET "
                "name=EXCLUDED.name, role_tag=EXCLUDED.role_tag, camera_id=EXCLUDED.camera_id",
                (zone_id, name, role_tag, camera_id or None, created_by, staffed),
            )

    def get(self, zone_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_zones WHERE zone_id=%s", (zone_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self, active_only: bool = False) -> list:
        where = "WHERE active = true" if active_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM floorwatch_zones {where} ORDER BY zone_id",
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_active(self, zone_id: str, active: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_zones SET active=%s WHERE zone_id=%s", (active, zone_id))
            return cur.rowcount > 0

    def set_staffed(self, zone_id: str, staffed: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_zones SET staffed=%s WHERE zone_id=%s", (staffed, zone_id))
            return cur.rowcount > 0


class JsonZoneDirectory:
    """Local-file fallback when FLOORWATCH_POSTGRES_DSN isn't set — same
    honest-fallback pattern as JsonEmployeeDirectory. Does NOT survive a
    Railway redeploy without a mounted volume."""

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

    def add(self, zone_id: str, name: str, role_tag: str, camera_id: Optional[str] = None,
            created_by: Optional[str] = None, staffed: bool = True):
        from datetime import datetime, timezone
        data = self._load()
        existing = data.get(zone_id, {})
        data[zone_id] = {
            "zone_id": zone_id, "name": name, "role_tag": role_tag, "camera_id": camera_id or None,
            "active": existing.get("active", True),
            "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "created_by": existing.get("created_by") or created_by,
            "staffed": existing.get("staffed", staffed),
        }
        self._save(data)

    def get(self, zone_id: str) -> Optional[dict]:
        return self._load().get(zone_id)

    def list_all(self, active_only: bool = False) -> list:
        entries = sorted(self._load().values(), key=lambda e: e["zone_id"])
        if active_only:
            entries = [e for e in entries if e.get("active", True)]
        return entries

    def set_active(self, zone_id: str, active: bool) -> bool:
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


def build_zone_directory(postgres_dsn: Optional[str], fallback_path: Path):
    """Same honest-fallback dispatch as build_employee_directory()."""
    if postgres_dsn:
        try:
            directory = PostgresZoneDirectory(postgres_dsn)
            _log(f"Using PostgresZoneDirectory at {postgres_dsn}")
            return directory
        except Exception as e:
            _log(f"could not connect to Postgres for zone directory ({e}) — falling back "
                 f"to local JSON. This will NOT survive a Railway redeploy without a mounted "
                 f"volume — see RAILWAY_DEPLOYMENT.md.", level="warning")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — zone directory using the local JSON "
             "file (dev/pilot fallback; won't survive a Railway redeploy without a volume).")
    return JsonZoneDirectory(fallback_path)
