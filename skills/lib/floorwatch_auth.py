"""Floorwatch shared authentication — per-supervisor login.

Built in response to the security review (SECURITY_REVIEW.md, finding
AUTH-1: every API endpoint and the live WebSocket were reachable with
zero credentials). This is the ONE source of truth for password hashing,
token issuance, and token verification, shared identically by
floorwatch-rules-engine and floorwatch-intelligence — never forked per
service, matching the same pattern as floorwatch_schema.py.

Design:
  - Passwords: PBKDF2-HMAC-SHA256 (stdlib `hashlib`, no extra dependency),
    random per-user salt, 200,000 iterations (OWASP 2023 minimum guidance
    for PBKDF2-SHA256). Stored as "salt_hex$digest_hex" — self-contained,
    no separate salt column/file needed to verify later.
  - Sessions: JWT (HS256), signed with one secret shared by both services
    so each can independently verify a token with no network call or
    shared session store between them. The secret is either supplied via
    FLOORWATCH_AUTH_SECRET, or auto-generated once and persisted to a
    file both services resolve to the same path (see get_or_create_secret)
    — convenient for local/dev runs, but a real deployment should set
    FLOORWATCH_AUTH_SECRET explicitly and keep it out of the repo tree.
  - Roles: "admin" (manages accounts, plus everything supervisor can do),
    "supervisor" (full read/write on coverage/tasks), "viewer" (read-only),
    "service" (used only by floorwatch-intelligence's self-issued token for
    its server-to-server reads of the rules engine — see that service's
    config.py. Deliberately issued at "viewer" scope, not a separate
    elevated tier, so a bug in that service's read-only tool layer can't
    accidentally mutate state even if it tried).
  - Role checks are HIERARCHICAL, not exact-match: admin > supervisor >
    viewer, via ROLE_RANK below. A required_role="supervisor" check passes
    for both supervisor and admin tokens; required_role="admin" passes only
    for admin. "service" is orthogonal to this ladder — always compares at
    viewer's rank, matching its documented read-only intent above.

Known limitations, flagged rather than silently glossed over:
  - No server-side revocation list. A stolen/leaked token remains valid
    until it expires (default 12h) — this now also applies to an admin
    DEACTIVATING a user: their already-issued token keeps working until it
    naturally expires, deactivation only blocks future logins immediately.
    A real deployment wanting instant revocation needs a token blocklist or
    a move to server-side sessions.
  - Account data lives in whichever store build_user_store() picks
    (Postgres if FLOORWATCH_POSTGRES_DSN is set, else the local JSON file)
    — same honest-fallback pattern as floorwatch-intelligence's
    vector_store.py. The JSON fallback does NOT survive a Railway redeploy
    without a mounted volume (see RAILWAY_DEPLOYMENT.md) — use Postgres for
    anything beyond local dev/testing.
"""

import hashlib
import hmac
import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt  # PyJWT

PBKDF2_ITERATIONS = 200_000
DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600

VALID_ROLES = {"admin", "supervisor", "viewer", "service"}
ROLE_RANK = {"viewer": 0, "service": 0, "supervisor": 1, "admin": 2}


def _log(msg: str):
    print(f"[floorwatch_auth] {msg}", file=sys.stderr, flush=True)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Password hashing ──────────────────────────────────────────────────────

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# ── Tokens ─────────────────────────────────────────────────────────────────

def issue_token(secret: str, username: str, role: str, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> str:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    now = int(time.time())
    payload = {"sub": username, "role": role, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_token(secret: str, token: str) -> Optional[dict]:
    """Returns the decoded payload, or None if invalid/expired/malformed — never raises."""
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_or_create_secret(path: Path) -> str:
    """Reads the shared signing secret from `path`, generating and persisting
    a new random one on first run. Both services must resolve to the SAME
    path for this to work without explicit FLOORWATCH_AUTH_SECRET config."""
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    return secret


# ── User store ─────────────────────────────────────────────────────────────
#
# Both stores below share the same interface (authenticate/create_user/
# user_exists/list_usernames/list_users/deactivate_user/reactivate_user/
# set_password/mark_password_changed/record_login), so main.py's admin
# endpoints and /api/login work identically regardless of which is active —
# same pattern as vector_store.py's SqliteVectorStore/PgVectorStore pair.

def _default_user_record(password_hash: str, role: str, created_by: Optional[str] = None) -> dict:
    return {
        "password_hash": password_hash,
        "role": role,
        "active": True,
        "must_change_password": True,
        "created_at": _utcnow_iso(),
        "created_by": created_by,
        "last_login_at": None,
    }


class UserStore:
    """users.json: {"username": {password_hash, role, active,
    must_change_password, created_at, created_by, last_login_at}}.

    Deliberately NOT shipped with any default/bootstrap credentials in the
    repo — a checked-in password hash is a checked-in secret. Operators run
    create_user.py to add the first (admin) account before starting the
    service. Local/dev fallback when FLOORWATCH_POSTGRES_DSN isn't set —
    does not survive a Railway redeploy without a mounted volume."""

    def __init__(self, users_path: Path):
        self.users_path = users_path

    def _load(self) -> dict:
        if not self.users_path.exists():
            return {}
        try:
            return json.loads(self.users_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, users: dict):
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        self.users_path.write_text(json.dumps(users, indent=2), encoding="utf-8")

    @staticmethod
    def _normalize(username: str, user: dict) -> dict:
        """Fills in defaults for accounts created before these fields
        existed — an old users.json entry only ever had password_hash/role."""
        return {
            "username": username,
            "role": user.get("role", "viewer"),
            "active": user.get("active", True),
            "must_change_password": user.get("must_change_password", False),
            "created_at": user.get("created_at"),
            "created_by": user.get("created_by"),
            "last_login_at": user.get("last_login_at"),
        }

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        """Returns {"role", "must_change_password"} if credentials are
        valid AND the account is active, else None. Deactivated accounts
        fail login immediately — see module docstring for why an
        already-issued token isn't also revoked immediately."""
        user = self._load().get(username)
        if user is None:
            return None
        if not user.get("active", True):
            return None
        if not verify_password(password, user.get("password_hash", "")):
            return None
        return {"role": user.get("role", "viewer"),
                "must_change_password": user.get("must_change_password", False)}

    def create_user(self, username: str, password: str, role: str = "supervisor",
                     created_by: Optional[str] = None, must_change_password: bool = True):
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        users = self._load()
        record = _default_user_record(hash_password(password), role, created_by)
        record["must_change_password"] = must_change_password
        users[username] = record
        self._save(users)

    def user_exists(self, username: str) -> bool:
        return username in self._load()

    def list_usernames(self) -> list:
        return sorted(self._load().keys())

    def list_users(self) -> list:
        """Admin-facing listing — never includes password_hash."""
        users = self._load()
        return [self._normalize(name, u) for name, u in sorted(users.items())]

    def set_active(self, username: str, active: bool) -> bool:
        users = self._load()
        if username not in users:
            return False
        users[username]["active"] = active
        self._save(users)
        return True

    def set_password(self, username: str, new_password: str, must_change_password: bool = True) -> bool:
        users = self._load()
        if username not in users:
            return False
        users[username]["password_hash"] = hash_password(new_password)
        users[username]["must_change_password"] = must_change_password
        self._save(users)
        return True

    def mark_password_changed(self, username: str):
        users = self._load()
        if username in users:
            users[username]["must_change_password"] = False
            self._save(users)

    def record_login(self, username: str):
        users = self._load()
        if username in users:
            users[username]["last_login_at"] = _utcnow_iso()
            self._save(users)


class PostgresUserStore:
    """Real Postgres implementation — reuses whatever Postgres instance is
    already configured (e.g. the same one floorwatch-intelligence's
    PgVectorStore points at via FLOORWATCH_POSTGRES_DSN) rather than
    standing up a second database; just a new table in it. Survives a
    Railway redeploy natively, unlike the JSON fallback above."""

    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS floorwatch_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT true,
            must_change_password BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by TEXT,
            last_login_at TIMESTAMPTZ
        );
    """

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL)

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash, role, active, must_change_password "
                "FROM floorwatch_users WHERE username=%s", (username,),
            ).fetchone()
        if row is None:
            return None
        password_hash, role, active, must_change_password = row
        if not active:
            return None
        if not verify_password(password, password_hash):
            return None
        return {"role": role, "must_change_password": must_change_password}

    def create_user(self, username: str, password: str, role: str = "supervisor",
                     created_by: Optional[str] = None, must_change_password: bool = True):
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO floorwatch_users "
                "(username, password_hash, role, active, must_change_password, created_by) "
                "VALUES (%s,%s,%s,true,%s,%s) "
                "ON CONFLICT (username) DO UPDATE SET "
                "password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, "
                "must_change_password=EXCLUDED.must_change_password",
                (username, hash_password(password), role, must_change_password, created_by),
            )

    def user_exists(self, username: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM floorwatch_users WHERE username=%s", (username,)).fetchone()
        return row is not None

    def list_usernames(self) -> list:
        with self._connect() as conn:
            rows = conn.execute("SELECT username FROM floorwatch_users ORDER BY username").fetchall()
        return [r[0] for r in rows]

    def list_users(self) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username, role, active, created_at, created_by, last_login_at "
                "FROM floorwatch_users ORDER BY username"
            ).fetchall()
        return [
            {"username": r[0], "role": r[1], "active": r[2],
             "created_at": r[3].isoformat() if r[3] else None, "created_by": r[4],
             "last_login_at": r[5].isoformat() if r[5] else None}
            for r in rows
        ]

    def set_active(self, username: str, active: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_users SET active=%s WHERE username=%s", (active, username))
            return cur.rowcount > 0

    def set_password(self, username: str, new_password: str, must_change_password: bool = True) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_users SET password_hash=%s, must_change_password=%s WHERE username=%s",
                (hash_password(new_password), must_change_password, username),
            )
            return cur.rowcount > 0

    def mark_password_changed(self, username: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE floorwatch_users SET must_change_password=false WHERE username=%s", (username,))

    def record_login(self, username: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE floorwatch_users SET last_login_at=now() WHERE username=%s", (username,))


def build_user_store(postgres_dsn: Optional[str], users_path: Path):
    """Tries Postgres first (reusing whatever instance is already
    configured — see PostgresUserStore's docstring for why this doesn't
    need a second database); falls back to the local JSON file
    automatically if unreachable/unconfigured. Same honest-fallback
    pattern as vector_store.py's build_vector_store()."""
    if postgres_dsn:
        try:
            store = PostgresUserStore(postgres_dsn)
            _log(f"Using PostgresUserStore at {postgres_dsn}")
            return store
        except Exception as e:
            _log(f"WARNING: could not connect to Postgres ({e}) — falling back to "
                 f"the local JSON user store. This will NOT survive a Railway "
                 f"redeploy without a mounted volume — see RAILWAY_DEPLOYMENT.md.")
    else:
        _log("No FLOORWATCH_POSTGRES_DSN configured — using the local JSON user store "
             "(dev/pilot fallback; won't survive a Railway redeploy without a volume).")
    return UserStore(users_path)


# ── FastAPI wiring (lazy import — this module has no hard fastapi dependency,
# so it stays importable from plain scripts like create_user.py) ───────────

def make_auth_dependency(secret: str, required_role: Optional[str] = None):
    """Builds a FastAPI dependency that validates the Bearer token on a
    request. required_role=None accepts any valid role. Otherwise the
    check is HIERARCHICAL via ROLE_RANK (admin > supervisor > viewer) —
    required_role="supervisor" accepts supervisor AND admin tokens;
    required_role="admin" accepts only admin tokens."""
    from fastapi import Header, HTTPException

    required_rank = ROLE_RANK.get(required_role) if required_role else None

    async def _dependency(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        token = authorization[len("Bearer "):]
        payload = verify_token(secret, token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if required_rank is not None and ROLE_RANK.get(payload.get("role"), -1) < required_rank:
            raise HTTPException(status_code=403, detail=f"{required_role.capitalize()} role required")
        return payload

    return _dependency


async def verify_ws_token(secret: str, websocket) -> Optional[dict]:
    """For WebSocket endpoints — browsers can't set custom headers on the
    native WebSocket API, so the token travels as a query param instead:
    wss://host/events?token=<jwt>. Returns the decoded payload or None."""
    token = websocket.query_params.get("token")
    if not token:
        return None
    return verify_token(secret, token)
