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
  - Revocation is opt-in and coarse. RevocationStore (below) lets a caller
    kill every token a username currently holds — wired into
    floorwatch-rules-engine on account deactivation and admin-forced
    password reset — but it's per-USERNAME, not per-token: there's no way
    to kill one specific session while leaving that user's other sessions
    alone. And it only takes effect wherever a RevocationStore was
    actually passed to make_auth_dependency()/verify_ws_token() — a
    service that doesn't wire one up (or doesn't share the same backing
    Redis instance as whichever service calls .revoke()) keeps accepting
    an otherwise-valid token for that revoked user until it naturally
    expires. See RevocationStore's own docstring for the full picture.
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
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt  # PyJWT

from floorwatch_retention import parse_timestamp  # noqa: E402

PBKDF2_ITERATIONS = 200_000
DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600

VALID_ROLES = {"admin", "supervisor", "viewer", "service"}
ROLE_RANK = {"viewer": 0, "service": 0, "supervisor": 1, "admin": 2}

# ── Password policy (DATA_PROTECTION_SECURITY_ANALYSIS.md DP-M1) ────────
# Deliberately NOT requiring arbitrary character-class complexity
# (uppercase+digit+symbol rules) — current guidance (NIST SP 800-63B)
# actively recommends against that: it pushes people toward predictable
# patterns like "Password1!" that satisfy the rule but aren't actually
# stronger, while a longer passphrase with no special characters is both
# easier to remember and harder to crack. What NIST recommends instead:
# a reasonable minimum length, and rejecting passwords already known to
# be common/breached. No network calls to a live breach-check API here
# (e.g. HaveIBeenPwned) — that's a real option for later, but it's an
# external dependency + a design decision (what happens if the check is
# unreachable — fail open or closed?) bigger than this pass; a local
# blocklist of the most commonly-used weak passwords is a genuine, real
# mitigation against the most trivial attack targets without that
# dependency, matching OWASP's "reject top-N common passwords" guidance.
MIN_PASSWORD_LENGTH = 10

# A representative sample of the passwords that show up at the very top
# of every public breach-corpus frequency analysis (RockYou and similar)
# — not exhaustive, but blocks the single most common brute-force/
# credential-stuffing targets for near-zero cost. Checked case-insensitively.
COMMON_WEAK_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "letmein123", "welcome123", "admin1234", "administrator",
    "iloveyou1", "sunshine1", "princess1", "football1", "baseball1", "dragon1234",
    "monkey123", "trustno1", "starwars1", "superman1", "batman123", "master123",
    "shadow123", "michael1", "jennifer1", "computer1", "changeme1", "letmein1234",
    "passw0rd", "p@ssword", "p@ssw0rd", "abcd1234", "abc12345", "a1b2c3d4",
    "1q2w3e4r", "zxcvbnm1", "asdfghjk", "qazwsx12", "1qaz2wsx", "0987654321",
}


def validate_password_strength(password: str, username: Optional[str] = None):
    """Returns (True, None) if the password passes policy, else
    (False, "human-readable reason"). Every password-setting code path —
    self-service change, admin create, admin reset — should call this
    instead of a bare len(password) < 8 check."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in COMMON_WEAK_PASSWORDS:
        return False, "That password is too common and easily guessed — choose a different one."
    if username and password.lower() == username.lower():
        return False, "Password cannot be the same as the username."
    return True, None


# ── Username policy (DATA_PROTECTION_SECURITY_ANALYSIS.md DP-M5) ────────
# CreateUserRequest previously had no length limit or character whitelist
# on username — only role validity, password length, and a duplicate
# check. This is the specific gap that made DP-H2 (username-field XSS)
# possible in the first place, and separately just inconsistent hygiene
# (nothing stopped an absurdly long username, one that's all whitespace,
# or one built from confusable/invisible Unicode). A plain ASCII
# whitelist is deliberately strict here — usernames are internal login
# handles for staff accounts an admin creates, not a user-chosen display
# name, so there's no real cost to disallowing spaces/punctuation/Unicode.
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")


def validate_username(username: str):
    """Returns (True, None) if the username passes policy, else
    (False, "human-readable reason"). Call before create_user() on any
    admin-facing account-creation path."""
    if not username or not (USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH):
        return False, f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters."
    if not USERNAME_PATTERN.match(username):
        return False, "Username may only contain letters, numbers, underscores, hyphens, and periods."
    return True, None


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


# ── Token revocation ──────────────────────────────────────────────────────
# Production-readiness gap: "no server-side token revocation — a stolen
# token stays valid until natural expiry (12h)." A JWT can't be
# invalidated by itself (that's the whole point of it being stateless) —
# real revocation necessarily means checking some piece of server-side
# state on every authenticated request, deliberately trading away part
# of a JWT's usual no-lookup benefit in exchange for actually being able
# to kill a compromised or deactivated account's existing session.
#
# Tracks a per-USERNAME cutoff timestamp, not a per-token blocklist —
# every token issued for that user at or before the cutoff is treated as
# revoked. Coarser than per-session revocation (there's no way to kill
# one specific browser tab while leaving that user's other sessions
# alone), but that's the right granularity for the two real triggers
# wired up in floorwatch-rules-engine/app/main.py: an admin deactivating
# an account, or forcibly resetting its password. Both mean "kill
# everything this user currently holds," never "kill this one session."
#
# Redis-backed so floorwatch-rules-engine (which triggers revocation) and
# floorwatch-intelligence (which never triggers it, but shares the same
# tokens and should still honor it) can check the same flag without a
# network call between them — see floorwatch-intelligence/app/config.py's
# REDIS_URL. Falls back to an in-process dict if no Redis client is given
# — same honest-fallback pattern as the rest of this codebase — but that
# fallback is per-process: it doesn't work across multiple replicas of
# the same service, or across two different services' processes at all.
# A deployment that actually needs revocation to work must pass a real
# Redis client here, not rely on the fallback.

class RevocationStore:
    _KEY_PREFIX = "floorwatch:revoked:"

    def __init__(self, redis_client=None, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS):
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._local: dict = {}

    async def revoke(self, username: str, now: Optional[int] = None):
        """Marks every token `username` currently holds as invalid from
        this moment on. Does NOT block them from logging in again — a
        fresh login issues a new token with a later `iat`, which is
        unaffected."""
        cutoff = now if now is not None else int(time.time())
        if self._redis is not None:
            await self._redis.set(f"{self._KEY_PREFIX}{username}", cutoff, ex=self._ttl)
        else:
            self._local[username] = cutoff

    async def is_revoked(self, username: str, issued_at: int) -> bool:
        if self._redis is not None:
            raw = await self._redis.get(f"{self._KEY_PREFIX}{username}")
            cutoff = int(raw) if raw is not None else None
        else:
            cutoff = self._local.get(username)
        return cutoff is not None and issued_at <= cutoff

    async def close(self):
        """Closes the underlying Redis connection pool, if any. Call this
        from the owning service's shutdown path (e.g. a FastAPI lifespan's
        post-yield block) — a redis.asyncio client left open past its
        event loop's lifetime is a real resource leak, and in a process
        that repeatedly rebuilds this store (tests re-importing the owning
        module many times in one process being the main example) those
        leaked connections can pile up and stall interpreter shutdown."""
        if self._redis is not None:
            await self._redis.aclose()


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
        "deactivated_at": None,
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
            "deactivated_at": user.get("deactivated_at"),
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
        # DP-M4: track when an account was deactivated so a later retention
        # pass can find deactivated-and-stale accounts; reactivating clears
        # it (the account is live again, not on a countdown to purge).
        users[username]["deactivated_at"] = None if active else _utcnow_iso()
        self._save(users)
        return True

    def delete_user(self, username: str) -> bool:
        """Hard delete — used only by the retention job (DP-M4) to purge
        accounts that have sat deactivated past the retention window, never
        by an admin-facing endpoint (deactivation is the reversible,
        admin-facing operation; this is not)."""
        users = self._load()
        if username not in users:
            return False
        del users[username]
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
            last_login_at TIMESTAMPTZ,
            deactivated_at TIMESTAMPTZ
        );
    """

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL)
            # DP-M4: added after this table already existed in real
            # deployments — CREATE TABLE IF NOT EXISTS above is a no-op
            # against an existing table, so the column needs an explicit
            # migration here too.
            conn.execute("ALTER TABLE floorwatch_users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;")

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
                "SELECT username, role, active, created_at, created_by, last_login_at, deactivated_at "
                "FROM floorwatch_users ORDER BY username"
            ).fetchall()
        return [
            {"username": r[0], "role": r[1], "active": r[2],
             "created_at": r[3].isoformat() if r[3] else None, "created_by": r[4],
             "last_login_at": r[5].isoformat() if r[5] else None,
             "deactivated_at": r[6].isoformat() if r[6] else None}
            for r in rows
        ]

    def set_active(self, username: str, active: bool) -> bool:
        # DP-M4: track when an account was deactivated (cleared back to
        # NULL on reactivation) so the retention job can find deactivated-
        # and-stale accounts. now() is evaluated server-side for the same
        # reason created_at's default does — consistent clock source.
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE floorwatch_users SET active=%s, "
                "deactivated_at = CASE WHEN %s THEN NULL ELSE now() END "
                "WHERE username=%s",
                (active, active, username),
            )
            return cur.rowcount > 0

    def delete_user(self, username: str) -> bool:
        """Hard delete — used only by the retention job (DP-M4), never by
        an admin-facing endpoint. See UserStore.delete_user's docstring."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM floorwatch_users WHERE username=%s", (username,))
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


# ── Account retention (DATA_PROTECTION_SECURITY_ANALYSIS.md DP-M4) ──────
# retention.py in both services pruned shift_digest.jsonl / incident notes
# but never touched account records — a deactivated account (and any
# personal data in created_by/last_login_at) persisted forever with no
# expiry path. This works against either store (UserStore or
# PostgresUserStore) since both implement the same list_users/
# set_active/delete_user interface — same polymorphism the rest of this
# module already relies on.

def purge_stale_deactivated_accounts(store, retention_days: int, archive_path: Optional[Path] = None,
                                       dry_run: bool = False, now: Optional[datetime] = None) -> dict:
    """Purges accounts that have been deactivated for more than
    retention_days. Never touches an active account, regardless of age —
    only accounts an admin has explicitly deactivated are eligible, and
    only once they've sat deactivated past the retention window. An
    account deactivated before this field existed (deactivated_at is
    missing/unparseable) is always kept — never guessed away, matching
    prune_jsonl_file's same rule for undated JSONL entries.

    If archive_path is given (and dry_run is False), purged records are
    appended there as JSONL before deletion — archived, not silently
    destroyed. Records never include password_hash (list_users() never
    returns it in the first place).

    Returns {"purged": N, "kept": N}.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    to_purge, kept = [], 0
    for user in store.list_users():
        if user.get("active", True):
            kept += 1
            continue
        deactivated_at = parse_timestamp(user.get("deactivated_at") or "")
        if deactivated_at is None or deactivated_at >= cutoff:
            kept += 1
            continue
        to_purge.append(user)

    if dry_run:
        return {"purged": len(to_purge), "kept": kept}

    if to_purge and archive_path is not None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path, "a", encoding="utf-8") as f:
            for user in to_purge:
                f.write(json.dumps(user) + "\n")

    for user in to_purge:
        store.delete_user(user["username"])

    return {"purged": len(to_purge), "kept": kept}


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

def make_auth_dependency(secret: str, required_role: Optional[str] = None,
                          revocation_store: Optional[RevocationStore] = None):
    """Builds a FastAPI dependency that validates the Bearer token on a
    request. required_role=None accepts any valid role. Otherwise the
    check is HIERARCHICAL via ROLE_RANK (admin > supervisor > viewer) —
    required_role="supervisor" accepts supervisor AND admin tokens;
    required_role="admin" accepts only admin tokens.

    revocation_store is optional — pass one (see RevocationStore above)
    to also reject a token belonging to a since-revoked username. Left
    unset (the default), this behaves exactly as before revocation
    existed: any structurally valid, unexpired token is accepted."""
    from fastapi import Header, HTTPException

    required_rank = ROLE_RANK.get(required_role) if required_role else None

    async def _dependency(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        token = authorization[len("Bearer "):]
        payload = verify_token(secret, token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if revocation_store is not None and await revocation_store.is_revoked(payload["sub"], payload["iat"]):
            raise HTTPException(status_code=401, detail="Token revoked — please log in again")
        if required_rank is not None and ROLE_RANK.get(payload.get("role"), -1) < required_rank:
            raise HTTPException(status_code=403, detail=f"{required_role.capitalize()} role required")
        return payload

    return _dependency


async def verify_ws_token(secret: str, websocket,
                           revocation_store: Optional[RevocationStore] = None) -> Optional[dict]:
    """For WebSocket endpoints — browsers can't set custom headers on the
    native WebSocket API, so the token travels as a query param instead:
    wss://host/events?token=<jwt>. Returns the decoded payload or None."""
    token = websocket.query_params.get("token")
    if not token:
        return None
    payload = verify_token(secret, token)
    if payload is None:
        return None
    if revocation_store is not None and await revocation_store.is_revoked(payload["sub"], payload["iat"]):
        return None
    return payload
