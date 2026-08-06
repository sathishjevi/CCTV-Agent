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
  - Roles: "supervisor" (full read/write), "viewer" (read-only), "service"
    (used only by floorwatch-intelligence's self-issued token for its
    server-to-server reads of the rules engine — see that service's
    config.py. Deliberately issued at "viewer" scope, not a separate
    elevated tier, so a bug in that service's read-only tool layer can't
    accidentally mutate state even if it tried).

Known limitation, flagged rather than silently glossed over: there is no
server-side revocation list in this pilot. A stolen/leaked token remains
valid until it expires (default 12h). A real deployment wanting instant
revocation needs a token blocklist or a move to server-side sessions.
"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Optional

import jwt  # PyJWT

PBKDF2_ITERATIONS = 200_000
DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600

VALID_ROLES = {"supervisor", "viewer", "service"}


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

class UserStore:
    """users.json: {"username": {"password_hash": "salt$digest", "role": "supervisor"|"viewer"}}

    Deliberately NOT shipped with any default/bootstrap credentials in the
    repo — a checked-in password hash is a checked-in secret. Operators run
    create_user.py to add the first account before starting the service."""

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

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Returns the user's role if credentials are valid, else None."""
        user = self._load().get(username)
        if user is None:
            return None
        if not verify_password(password, user.get("password_hash", "")):
            return None
        return user.get("role", "viewer")

    def create_user(self, username: str, password: str, role: str = "supervisor"):
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        users = self._load()
        users[username] = {"password_hash": hash_password(password), "role": role}
        self._save(users)

    def user_exists(self, username: str) -> bool:
        return username in self._load()

    def list_usernames(self) -> list:
        return sorted(self._load().keys())


# ── FastAPI wiring (lazy import — this module has no hard fastapi dependency,
# so it stays importable from plain scripts like create_user.py) ───────────

def make_auth_dependency(secret: str, required_role: Optional[str] = None):
    """Builds a FastAPI dependency that validates the Bearer token on a
    request. required_role=None accepts any valid role (supervisor, viewer,
    or service); required_role="supervisor" additionally rejects
    viewer/service tokens with 403."""
    from fastapi import Header, HTTPException

    async def _dependency(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        token = authorization[len("Bearer "):]
        payload = verify_token(secret, token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if required_role == "supervisor" and payload.get("role") != "supervisor":
            raise HTTPException(status_code=403, detail="Supervisor role required")
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
