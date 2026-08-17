"""Floorwatch coverage rules engine — FastAPI service.

Consumes schema-validated zone_covered/zone_gap events from the Redis
Stream floorwatch-coverage publishes to, runs them through the Tier 1/2/3
escalation state machine (engine.py — Part B) and the active-time/effort
state machine (effort_engine.py — Part A, fed by floorwatch-pose's motion
signal), and broadcasts the resulting events over a WebSocket the
dashboard connects to — replacing the demo's script[]/run() simulator per
docs/floorwatch/Floorwatch_CCTV_Integration_Pipeline.docx Section 3.

Run:
  uvicorn main:app --reload --port 8080
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402 (also inserts skills/lib onto sys.path — see config.py)
from digest_store import DigestStore  # noqa: E402
from effort_engine import EffortEngine  # noqa: E402
from engine import RulesEngine  # noqa: E402
from notifications import ContactBook, NotificationDispatcher, build_sender  # noqa: E402
from roster import Roster  # noqa: E402

from floorwatch_auth import (  # noqa: E402
    VALID_ROLES, RevocationStore, build_user_store, issue_token, make_auth_dependency,
    validate_password_strength, validate_username, verify_ws_token,
)
from floorwatch_rate_limit import RateLimiter  # noqa: E402
from floorwatch_schema import validate_event  # noqa: E402
from floorwatch_security_headers import install_security_headers  # noqa: E402

users = build_user_store(config.POSTGRES_DSN, config.USERS_PATH)

# Token revocation (production-readiness: "no server-side token
# revocation") — Redis-backed since this service already requires Redis
# for its event/motion streams, so this adds no new infrastructure. See
# floorwatch_auth.py's RevocationStore docstring for exactly what this
# does and doesn't cover.
import redis.asyncio as aioredis  # noqa: E402
revocation_store = RevocationStore(
    aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True), ttl_seconds=config.TOKEN_TTL_SECONDS)

require_auth = make_auth_dependency(config.AUTH_SECRET, revocation_store=revocation_store)  # any valid role
require_supervisor = make_auth_dependency(
    config.AUTH_SECRET, required_role="supervisor", revocation_store=revocation_store)  # supervisor or admin
require_admin = make_auth_dependency(
    config.AUTH_SECRET, required_role="admin", revocation_store=revocation_store)

# DATA_PROTECTION_SECURITY_ANALYSIS.md DP-H3 — see config.py's comment
# above these three env vars for the per-IP vs per-username rationale.
login_rate_limiter_by_ip = RateLimiter(config.LOGIN_RATE_LIMIT_PER_IP_PER_MINUTE, window_seconds=60.0)
login_rate_limiter_by_username = RateLimiter(config.LOGIN_RATE_LIMIT_PER_USERNAME_PER_MINUTE, window_seconds=60.0)
admin_rate_limiter = RateLimiter(config.ADMIN_RATE_LIMIT_PER_MINUTE, window_seconds=60.0)


def client_ip(request: Request) -> str:
    """Prefers X-Forwarded-For (Railway and most reverse proxies set this
    — request.client.host alone would just be the proxy's own address,
    making every caller look identical for rate-limiting purposes).
    Takes the leftmost address (the original client, per the standard
    left-to-right append order of that header) and falls back to
    request.client.host if the header isn't present (e.g. local dev,
    direct connection)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def log(msg: str):
    print(f"[rules-engine] {msg}", file=sys.stderr, flush=True)


def _seed_admin_from_env():
    """FLOORWATCH_ADMIN_USERNAME/FLOORWATCH_ADMIN_PASSWORD — an alternative
    bootstrap to running create_user.py/generate_admin_sql.py by hand (see
    config.py's ADMIN_USERNAME docstring for the full contract). Only ever
    CREATES the account if it's missing — never touches an existing one, so
    changing the real password later (forced on first login, like any other
    admin-created account) survives every future redeploy even with these
    same env vars still set."""
    if not (config.ADMIN_USERNAME and config.ADMIN_PASSWORD):
        return
    if users.user_exists(config.ADMIN_USERNAME):
        log(f"FLOORWATCH_ADMIN_USERNAME='{config.ADMIN_USERNAME}' is set but that account already "
            f"exists — not touching its password/role. Unset these env vars once you no longer need "
            f"them, or leave them — they're harmless from here on.")
        return
    ok, reason = validate_password_strength(config.ADMIN_PASSWORD, username=config.ADMIN_USERNAME)
    if not ok:
        log(f"WARNING: FLOORWATCH_ADMIN_PASSWORD rejected ({reason}) — "
            f"refusing to seed account '{config.ADMIN_USERNAME}'.")
        return
    users.create_user(config.ADMIN_USERNAME, config.ADMIN_PASSWORD, role="admin",
                       created_by="env:FLOORWATCH_ADMIN_USERNAME", must_change_password=True)
    log(f"Seeded initial admin account '{config.ADMIN_USERNAME}' from "
        f"FLOORWATCH_ADMIN_USERNAME/FLOORWATCH_ADMIN_PASSWORD — forced to set a real password on first login.")


_seed_admin_from_env()


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, event: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
zones_meta = json.loads(config.ZONES_META_PATH.read_text()) if config.ZONES_META_PATH.exists() else {}
task_type_thresholds = (json.loads(config.TASK_TYPE_THRESHOLDS_PATH.read_text())
                         if config.TASK_TYPE_THRESHOLDS_PATH.exists() else {"_default": {"expected_active_ratio": 0.5}})
roster = Roster(config.ROSTER_PATH)
digest = DigestStore(config.DIGEST_PATH)

contacts = ContactBook(config.CONTACTS_PATH)
notify_sender = build_sender(config.NOTIFY_CHANNEL, config)
notify_dispatcher = NotificationDispatcher(notify_sender, contacts)
log(f"Notification channel: {config.NOTIFY_CHANNEL} (sender={type(notify_sender).__name__}), "
    f"shadow_mode={config.SHADOW_MODE} — real sends only happen when both a real channel is "
    f"configured AND shadow_mode is false.")

engine = RulesEngine(
    roster=roster,
    digest=digest,
    zones_meta=zones_meta,
    shadow_mode=config.SHADOW_MODE,
    nudge_timer_seconds=config.NUDGE_TIMER_SECONDS,
    nudge_cap_per_shift=config.NUDGE_CAP_PER_SHIFT,
    command_timer_seconds=config.COMMAND_TIMER_SECONDS,
    command_throttle_per_hour=config.COMMAND_THROTTLE_PER_HOUR,
    resolve_cooldown_seconds=config.RESOLVE_COOLDOWN_SECONDS,
    on_notify=notify_dispatcher,
)


def _zone_is_covered(zone_id: str) -> bool:
    # A zone the Part B engine hasn't seen any event for yet defaults to
    # "covered" — same default ZoneRuntime itself starts with.
    z = engine.zones.get(zone_id)
    return z.status == "covered" if z is not None else True


effort_engine = EffortEngine(
    roster=roster,
    digest=digest,
    zones_meta=zones_meta,
    task_type_thresholds=task_type_thresholds,
    shadow_mode=config.SHADOW_MODE,
    update_interval_seconds=config.EFFORT_UPDATE_INTERVAL_SECONDS,
    max_motion_gap_seconds=config.EFFORT_MAX_MOTION_GAP_SECONDS,
    nudge_grace_ratio=config.EFFORT_NUDGE_GRACE_RATIO,
    nudge_margin=config.EFFORT_NUDGE_MARGIN,
    zone_is_covered=_zone_is_covered,
    on_notify=notify_dispatcher,
)


async def redis_consumer_loop():
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        await client.xgroup_create(config.REDIS_STREAM, config.REDIS_CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            log(f"WARNING: could not create consumer group: {e}")

    log(f"Consuming Redis stream '{config.REDIS_STREAM}' as group '{config.REDIS_CONSUMER_GROUP}'")
    while True:
        try:
            resp = await client.xreadgroup(
                config.REDIS_CONSUMER_GROUP, config.REDIS_CONSUMER_NAME,
                {config.REDIS_STREAM: ">"}, count=10, block=2000,
            )
        except Exception as e:
            log(f"Redis read error: {e} — retrying in 2s")
            await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                try:
                    raw = json.loads(fields["data"])
                except (KeyError, json.JSONDecodeError):
                    log(f"Dropping malformed stream entry {entry_id}")
                    await client.xack(config.REDIS_STREAM, config.REDIS_CONSUMER_GROUP, entry_id)
                    continue

                validated = validate_event(raw, log=log)
                if validated is None:
                    await client.xack(config.REDIS_STREAM, config.REDIS_CONSUMER_GROUP, entry_id)
                    continue

                out_events = engine.process_detection_event(validated.model_dump())
                for evt in out_events:
                    await manager.broadcast(evt)
                await client.xack(config.REDIS_STREAM, config.REDIS_CONSUMER_GROUP, entry_id)


async def motion_consumer_loop():
    """Consumes floorwatch-pose's motion signal (its own stream — not the
    Section-2 schema, same as `detections` isn't schema-validated either;
    only the derived task_* events that come out of EffortEngine are)."""
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        await client.xgroup_create(config.REDIS_MOTION_STREAM, config.REDIS_MOTION_CONSUMER_GROUP,
                                    id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            log(f"WARNING: could not create motion consumer group: {e}")

    log(f"Consuming Redis motion stream '{config.REDIS_MOTION_STREAM}' "
        f"as group '{config.REDIS_MOTION_CONSUMER_GROUP}'")
    while True:
        try:
            resp = await client.xreadgroup(
                config.REDIS_MOTION_CONSUMER_GROUP, config.REDIS_CONSUMER_NAME,
                {config.REDIS_MOTION_STREAM: ">"}, count=20, block=2000,
            )
        except Exception as e:
            log(f"Redis motion read error: {e} — retrying in 2s")
            await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                try:
                    raw = json.loads(fields["data"])
                    effort_engine.record_motion(raw["camera_id"], bool(raw.get("active")))
                except (KeyError, json.JSONDecodeError, TypeError) as e:
                    log(f"Dropping malformed motion entry {entry_id}: {e}")
                await client.xack(config.REDIS_MOTION_STREAM, config.REDIS_MOTION_CONSUMER_GROUP, entry_id)


async def tick_loop():
    while True:
        await asyncio.sleep(config.TICK_INTERVAL_SECONDS)
        for evt in engine.tick():
            await manager.broadcast(evt)
        for evt in effort_engine.tick():
            await manager.broadcast(evt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    consumer_task = asyncio.create_task(redis_consumer_loop())
    motion_task = asyncio.create_task(motion_consumer_loop())
    ticker_task = asyncio.create_task(tick_loop())
    log(f"Rules engine started. shadow_mode={config.SHADOW_MODE}")
    yield
    consumer_task.cancel()
    motion_task.cancel()
    ticker_task.cancel()
    await revocation_store.close()


app = FastAPI(
    title="Floorwatch Rules Engine",
    lifespan=lifespan,
    docs_url="/docs" if config.DOCS_ENABLED else None,
    redoc_url="/redoc" if config.DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)
install_security_headers(app)  # DP-M2 — see floorwatch_security_headers.py


COVERAGE_UI_PATH = config.REPO_ROOT / "dashboard" / "floorwatch_demo.html"


@app.get("/")
async def coverage_ui():
    if COVERAGE_UI_PATH.exists():
        return FileResponse(str(COVERAGE_UI_PATH))
    return JSONResponse(status_code=404, content={"error": "coverage dashboard not found"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "shadow_mode": config.SHADOW_MODE, "connections": len(manager.active)}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginRequest, request: Request):
    ip = client_ip(request)
    if not login_rate_limiter_by_ip.allow(ip):
        retry_after = login_rate_limiter_by_ip.retry_after_seconds(ip)
        return JSONResponse(
            status_code=429, content={"error": "Too many login attempts — try again shortly."},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    if not login_rate_limiter_by_username.allow(body.username):
        retry_after = login_rate_limiter_by_username.retry_after_seconds(body.username)
        return JSONResponse(
            status_code=429, content={"error": "Too many login attempts for this account — try again shortly."},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    auth_result = users.authenticate(body.username, body.password)
    if auth_result is None:
        return JSONResponse(status_code=401, content={"error": "invalid username or password"})
    role = auth_result["role"]
    users.record_login(body.username)
    token = issue_token(config.AUTH_SECRET, body.username, role, ttl_seconds=config.TOKEN_TTL_SECONDS)
    return {
        "token": token, "username": body.username, "role": role,
        "expires_in": config.TOKEN_TTL_SECONDS,
        "must_change_password": auth_result["must_change_password"],
    }


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/change-password")
async def change_password(body: ChangePasswordRequest, user=Depends(require_auth)):
    """Self-service — any authenticated user changes their own password.
    Requires the current password (not just a valid token) so a
    briefly-unattended logged-in session can't be used to lock the real
    owner out by someone else changing it."""
    username = user["sub"]
    auth_result = users.authenticate(username, body.current_password)
    if auth_result is None:
        return JSONResponse(status_code=401, content={"error": "current password is incorrect"})
    ok, reason = validate_password_strength(body.new_password, username=username)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    users.set_password(username, body.new_password, must_change_password=False)
    return {"ok": True}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "supervisor"


def _check_admin_rate_limit(user) -> "JSONResponse | None":
    """Shared by every /api/admin/users* handler — bounds a compromised
    or malicious admin token from bulk-creating/enumerating/deactivating
    accounts unbounded (DP-H3's admin-endpoints half). Returns a 429
    response if over budget, else None (caller proceeds normally)."""
    if not admin_rate_limiter.allow(user["sub"]):
        retry_after = admin_rate_limiter.retry_after_seconds(user["sub"])
        return JSONResponse(
            status_code=429, content={"error": "Too many admin actions — try again shortly."},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    return None


@app.get("/api/admin/users")
async def list_users(user=Depends(require_admin)):
    if (limited := _check_admin_rate_limit(user)) is not None:
        return limited
    return {"users": users.list_users()}


@app.post("/api/admin/users")
async def create_user_endpoint(body: CreateUserRequest, user=Depends(require_admin)):
    if (limited := _check_admin_rate_limit(user)) is not None:
        return limited
    if body.role not in VALID_ROLES - {"service"}:
        return JSONResponse(status_code=400, content={
            "error": f"role must be one of: {sorted(VALID_ROLES - {'service'})}"})
    ok, reason = validate_username(body.username)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    ok, reason = validate_password_strength(body.password, username=body.username)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    if users.user_exists(body.username):
        return JSONResponse(status_code=409, content={"error": f"user '{body.username}' already exists"})
    users.create_user(body.username, body.password, role=body.role, created_by=user["sub"])
    log(f"Admin '{user['sub']}' created account '{body.username}' with role '{body.role}'")
    return {"ok": True}


@app.post("/api/admin/users/{username}/deactivate")
async def deactivate_user(username: str, user=Depends(require_admin)):
    if (limited := _check_admin_rate_limit(user)) is not None:
        return limited
    if username == user["sub"]:
        return JSONResponse(status_code=400, content={"error": "cannot deactivate your own account"})
    if not users.set_active(username, False):
        return JSONResponse(status_code=404, content={"error": f"user '{username}' not found"})
    # Deactivation should mean "this account can't act anymore," not just
    # "can't log in again" — kill any token they're already holding too.
    await revocation_store.revoke(username)
    log(f"Admin '{user['sub']}' deactivated account '{username}'")
    return {"ok": True}


@app.post("/api/admin/users/{username}/reactivate")
async def reactivate_user(username: str, user=Depends(require_admin)):
    if (limited := _check_admin_rate_limit(user)) is not None:
        return limited
    if not users.set_active(username, True):
        return JSONResponse(status_code=404, content={"error": f"user '{username}' not found"})
    log(f"Admin '{user['sub']}' reactivated account '{username}'")
    return {"ok": True}


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/api/admin/users/{username}/reset-password")
async def reset_password(username: str, body: ResetPasswordRequest, user=Depends(require_admin)):
    """Admin sets a temporary password and shares it with the user
    out-of-band (Slack, in person, etc.) — no email system in this
    codebase. must_change_password is forced true so the temp password
    can't quietly become permanent."""
    if (limited := _check_admin_rate_limit(user)) is not None:
        return limited
    ok, reason = validate_password_strength(body.new_password, username=username)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    if not users.set_password(username, body.new_password, must_change_password=True):
        return JSONResponse(status_code=404, content={"error": f"user '{username}' not found"})
    # An admin-forced reset is the "I think this account may be
    # compromised" lever — kill whatever token they're currently holding,
    # not just their old password. Deliberately NOT applied to self-service
    # /api/change-password below: that caller already re-proved they hold
    # both a valid token AND the current password, so there's no reason to
    # log out the very session that just made this change.
    await revocation_store.revoke(username)
    log(f"Admin '{user['sub']}' reset the password for account '{username}'")
    return {"ok": True}


@app.get("/api/state")
async def get_state(user=Depends(require_auth)):
    return {
        zone_id: {
            "status": z.status,
            "camera_id": z.camera_id,
            "role_tag": z.role_tag,
            "nudge_count_shift": z.nudge_count_shift,
        }
        for zone_id, z in engine.zones.items()
    }


@app.get("/api/digest")
async def get_digest(user=Depends(require_auth)):
    return digest.read_all()


@app.get("/api/queue")
async def get_queue(user=Depends(require_auth)):
    return engine.pending_commands()


@app.post("/api/queue/zone/{zone_id}/approve")
async def approve_zone(zone_id: str, user=Depends(require_supervisor)):
    evt = engine.approve(zone_id, supervisor_id=user["sub"])
    if evt is None:
        return JSONResponse(status_code=404, content={"error": "unknown zone or nothing pending"})
    await manager.broadcast(evt)
    return evt


@app.post("/api/queue/zone/{zone_id}/reassign")
async def reassign_zone(zone_id: str, user=Depends(require_supervisor)):
    evt = engine.reassign(zone_id, supervisor_id=user["sub"])
    if evt is None:
        return JSONResponse(status_code=404, content={"error": "unknown zone"})
    await manager.broadcast(evt)
    return evt


class TaskAssignRequest(BaseModel):
    task_name: str
    zone_id: str
    assigned_minutes: float
    task_type: str | None = None


@app.post("/api/tasks")
async def assign_task(body: TaskAssignRequest, user=Depends(require_supervisor)):
    evt = effort_engine.assign_task(body.task_name, body.zone_id, body.assigned_minutes, body.task_type)
    if evt is None:
        return JSONResponse(status_code=400, content={
            "error": f"zone '{body.zone_id}' is not staffed per the roster — task not created"})
    await manager.broadcast(evt)
    return evt


@app.get("/api/tasks")
async def list_tasks(user=Depends(require_auth)):
    now = effort_engine._clock()
    return {
        task_id: {
            "task_name": t.task_name,
            "task_type": t.task_type,
            "zone_id": t.zone_id,
            "zone_name": effort_engine._zone_label(t.zone_id),
            "status": t.status,
            "assigned_minutes": t.assigned_minutes,
            "active_minutes": round(t.active_seconds / 60.0, 2),
            "elapsed_minutes": round((now - t.start_monotonic) / 60.0, 2),
        }
        for task_id, t in effort_engine.tasks.items()
    }


@app.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: str, user=Depends(require_supervisor)):
    evt = effort_engine.complete_task(task_id)
    if evt is None:
        return JSONResponse(status_code=404, content={"error": "unknown task or already completed"})
    await manager.broadcast(evt)
    return evt


@app.get("/api/queue/tasks")
async def get_task_queue(user=Depends(require_auth)):
    return effort_engine.pending_flags()


@app.post("/api/queue/task/{task_id}/confirm")
async def confirm_task_flag(task_id: str, user=Depends(require_supervisor)):
    evt = effort_engine.confirm_flag(task_id, supervisor_id=user["sub"])
    if evt is None:
        return JSONResponse(status_code=404, content={"error": "unknown task or nothing pending"})
    await manager.broadcast(evt)
    return evt


@app.post("/api/queue/task/{task_id}/dismiss")
async def dismiss_task_flag(task_id: str, user=Depends(require_supervisor)):
    evt = effort_engine.dismiss_flag(task_id, supervisor_id=user["sub"])
    if evt is None:
        return JSONResponse(status_code=404, content={"error": "unknown task or nothing pending"})
    await manager.broadcast(evt)
    return evt


@app.websocket("/events")
async def events_ws(ws: WebSocket):
    payload = await verify_ws_token(config.AUTH_SECRET, ws, revocation_store=revocation_store)
    if payload is None:
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # dashboard doesn't send anything; just keep the connection open
    except WebSocketDisconnect:
        manager.disconnect(ws)
