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
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402 (also inserts skills/lib onto sys.path — see config.py)
from digest_store import DigestStore  # noqa: E402
from effort_engine import EffortEngine  # noqa: E402
from employee_directory import (  # noqa: E402
    build_employee_directory, validate_channel, validate_employee_number,
    validate_phone, validate_primary_contact,
)
from event_history import build_event_history_store  # noqa: E402
from engine import RulesEngine  # noqa: E402
from notifications import ContactBook, NotificationDispatcher, _mask_phone, build_sender  # noqa: E402
from roster import Roster  # noqa: E402
from sms_webhook import parse_sms_command, reply_twiml, validate_signature  # noqa: E402
from task_store import build_task_store, rehydrate_tasks, task_runtime_to_record  # noqa: E402

from floorwatch_auth import (  # noqa: E402
    VALID_ROLES, RevocationStore, build_user_store, issue_token, make_auth_dependency,
    validate_password_strength, validate_username, verify_ws_token,
)
from floorwatch_logging import get_logger  # noqa: E402
from floorwatch_rate_limit import RateLimiter  # noqa: E402
from floorwatch_schema import validate_event  # noqa: E402
from floorwatch_security_headers import install_security_headers  # noqa: E402
from cluster_bus import (  # noqa: E402
    broadcast_subscriber_loop, consume_commands_loop, publish_event, read_snapshot,
    submit_command, write_snapshot,
)
from leader_election import LeaderElection, leadership_loop  # noqa: E402

log = get_logger("rules-engine")

users = build_user_store(config.POSTGRES_DSN, config.USERS_PATH)

# Shared Redis client for every piece of cross-replica coordination this
# service needs — token revocation, leader election, state snapshots, the
# command bus, and the broadcast fan-out (see cluster_bus.py and
# leader_election.py). One client, not one per concern — this service
# already requires Redis for its event/motion streams, so consolidating
# onto a single connection adds no new infrastructure and matches how
# revocation_store already worked before this.
import redis.asyncio as aioredis  # noqa: E402
cluster_redis = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)

# Unique per PROCESS (not per request) — identifies this replica for
# leader election and for its own broadcast-fan-out consumer group. A
# random UUID is sufficient; nothing needs it to be human-meaningful.
REPLICA_ID = str(uuid.uuid4())

revocation_store = RevocationStore(cluster_redis, ttl_seconds=config.TOKEN_TTL_SECONDS)

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
        log(f"FLOORWATCH_ADMIN_PASSWORD rejected ({reason}) — "
            f"refusing to seed account '{config.ADMIN_USERNAME}'.", level="warning")
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
event_history = build_event_history_store(config.POSTGRES_DSN, config.EVENT_HISTORY_PATH)
employee_directory = build_employee_directory(config.POSTGRES_DSN, config.EMPLOYEE_DIRECTORY_PATH)
task_store = build_task_store(config.POSTGRES_DSN, config.TASK_STORE_PATH)

# TaskRuntime only holds a MONOTONIC start (meaningless across a
# restart) — this tracks the real wall-clock start per task_id so
# task_store persists something rehydrate_tasks() can actually use.
# Populated on first assignment and by rehydration itself on startup.
_task_started_at: dict = {}

# Heartbeat-style events excluded from durable history — see
# event_history.py's module docstring for why.
EVENT_HISTORY_EXCLUDED_TYPES = {"task_active_time_update"}

contacts = ContactBook(config.CONTACTS_PATH)
notify_sender = build_sender(config.NOTIFY_CHANNEL, config)
notify_dispatcher = NotificationDispatcher(notify_sender, contacts)
log(f"Notification channel: {config.NOTIFY_CHANNEL} (sender={type(notify_sender).__name__}), "
    f"shadow_mode={config.SHADOW_MODE} — real sends only happen when both a real channel is "
    f"configured AND shadow_mode is false.")

# ── Per-employee notification channel routing (task-workflow messaging
# only — the ContactBook/notify_sender pair above is Part A/B's
# anonymous zone-level nudge/directive path and is deliberately left
# untouched; see PHASE_2_NOTES.md). Both built unconditionally at
# startup, independent of the global NOTIFY_CHANNEL toggle, since an
# employee's own `channel` field can select either one regardless of
# what the deployment's global default is. build_sender() never raises —
# an unconfigured/misconfigured channel here just resolves to a
# NoOpSender, same fallback behavior as the global sender above.
TASK_CHANNEL_SENDERS = {
    "sms": build_sender("twilio", config),
    "fcm": build_sender("fcm", config),
}
# What an employee record with no `channel` override falls back to —
# preserves pre-Feature-1 behavior (always SMS) for every employee added
# before this field existed, per the brief's explicit backward-
# compatibility requirement.
_NOTIFY_CHANNEL_TO_TASK_CHANNEL = {"twilio": "sms", "fcm": "fcm"}
DEFAULT_TASK_CHANNEL = _NOTIFY_CHANNEL_TO_TASK_CHANNEL.get(config.NOTIFY_CHANNEL)

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


# ── Horizontal scaling: shared read-state + event fan-out ───────────────
# Production-readiness gap: "the WebSocket connection manager and
# in-memory state assume exactly one running instance." engine/
# effort_engine above stay exactly as they were — private, in-process
# Python objects — but only the current LEADER (see leader_election.py)
# ever calls their mutating methods now. Every replica's GET endpoints
# read the snapshots below instead of touching engine/effort_engine
# directly, so they answer identically regardless of which replica
# processed the underlying event.
SNAPSHOT_STATE_KEY = "floorwatch:snapshot:state"
SNAPSHOT_QUEUE_KEY = "floorwatch:snapshot:queue"
SNAPSHOT_TASKS_KEY = "floorwatch:snapshot:tasks"
SNAPSHOT_QUEUE_TASKS_KEY = "floorwatch:snapshot:queue_tasks"


async def _refresh_snapshots():
    """Called by the LEADER after any state change (stream-driven or
    command-driven) and on every tick — recomputes exactly what each GET
    endpoint used to compute inline from engine/effort_engine, and writes
    it where every replica can read it."""
    state = {
        zone_id: {"status": z.status, "camera_id": z.camera_id, "role_tag": z.role_tag,
                   "nudge_count_shift": z.nudge_count_shift}
        for zone_id, z in engine.zones.items()
    }
    now = effort_engine._clock()
    tasks = {
        task_id: {"task_name": t.task_name, "task_type": t.task_type, "zone_id": t.zone_id,
                  "zone_name": effort_engine._zone_label(t.zone_id), "status": t.status,
                  "assigned_minutes": t.assigned_minutes,
                  "active_minutes": round(t.active_seconds / 60.0, 2),
                  "elapsed_minutes": round((now - t.start_monotonic) / 60.0, 2),
                  "assigned_to": t.assigned_to, "assigned_by": t.assigned_by,
                  "workflow_status": t.workflow_status, "short_code": effort_engine.short_code(task_id),
                  "reopened_for_review": t.reopened_for_review}
        for task_id, t in effort_engine.tasks.items()
    }
    await write_snapshot(cluster_redis, SNAPSHOT_STATE_KEY, state)
    await write_snapshot(cluster_redis, SNAPSHOT_QUEUE_KEY, engine.pending_commands())
    await write_snapshot(cluster_redis, SNAPSHOT_TASKS_KEY, tasks)
    await write_snapshot(cluster_redis, SNAPSHOT_QUEUE_TASKS_KEY, effort_engine.pending_flags())


async def _emit(evt: dict):
    """Every event the engine produces goes through here instead of a
    direct manager.broadcast() call — refreshes the shared snapshots
    (BEFORE publishing, so by the time any replica's WebSocket clients
    see the event, a concurrent GET on any replica already reflects it),
    fans the event out over the broadcast stream so every replica's
    locally-connected WebSocket clients receive it (not just this
    leader's own), and durably records it to event_history (except
    heartbeat-style updates — see EVENT_HISTORY_EXCLUDED_TYPES). Only
    ever called by the leader — see consume_commands_loop/
    redis_consumer_loop/motion_consumer_loop/tick_loop, all leader-gated
    in lifespan()."""
    await _refresh_snapshots()
    await publish_event(cluster_redis, evt)
    if evt.get("event_type") not in EVENT_HISTORY_EXCLUDED_TYPES:
        try:
            # event_history.record() is a plain synchronous call (same
            # blocking-Postgres-call pattern already used throughout this
            # codebase, e.g. users.authenticate()) — run off the event
            # loop since this fires on every single event, not just once
            # per request.
            await asyncio.to_thread(event_history.record, evt)
        except Exception as e:
            log(f"could not record event to durable history: {e}", level="warning")

    task_id = evt.get("task_id")
    if task_id and task_id in effort_engine.tasks:
        try:
            t = effort_engine.tasks[task_id]
            started_at_iso = _task_started_at.setdefault(task_id, datetime.now(timezone.utc).isoformat())
            await asyncio.to_thread(task_store.upsert, task_runtime_to_record(t, started_at_iso))
        except Exception as e:
            log(f"could not persist task state: {e}", level="warning")


# ── Employee messaging — the outbound half of the task workflow ─────────

def _resolve_task_channel(employee: dict) -> str | None:
    """Feature 1 — per-employee channel routing. An employee's own
    `channel` field always wins; a record that predates this field (or
    was added without specifying one) falls back to DEFAULT_TASK_CHANNEL,
    which mirrors whatever the deployment's global NOTIFY_CHANNEL already
    meant before per-employee channels existed — see the comment above
    TASK_CHANNEL_SENDERS' definition. Returns None if there's no channel
    to use at all (no override AND no usable global default)."""
    return employee.get("channel") or DEFAULT_TASK_CHANNEL


def _send_task_notification(employee_number, message) -> dict:
    """Synchronous — always run via asyncio.to_thread from async callers,
    same as event_history.record(). Global Constraint 4 applies here
    exactly as it does to the zone/effort engines' own on_notify() path:
    shadow mode suppresses the real send but the caller still treats it
    as delivered for workflow-transition purposes, so the state machine
    behaves identically whether or not a real SMS account is configured.

    Explicitly does NOT fall back to a different channel than the one
    configured for this employee — a supervisor whose record says
    channel="fcm" but has no fcm_token on file gets a clear
    "no fcm token on file" skip, never a silent SMS instead (which could
    go to a stale/wrong number) or vice versa. Missing delivery detail
    is a real gap a human needs to see and fix, not paper over."""
    if not employee_number:
        return {"sent": False, "channel": "none", "detail": "no assignee"}
    employee = employee_directory.get(employee_number)
    if employee is None:
        return {"sent": False, "channel": "none", "detail": "employee not found in directory"}
    channel = _resolve_task_channel(employee)
    if not channel:
        log(f"No notification channel configured for employee {employee_number} "
            f"(no per-employee override and no usable global default) — skipping send", level="warning")
        return {"sent": False, "channel": "none", "detail": "no channel configured for this employee"}
    if config.SHADOW_MODE:
        dest = _mask_phone(employee.get("phone")) if channel == "sms" else "fcm token on file"
        log(f"SHADOW MODE — would message employee {employee_number} via {channel} ({dest}): {message}")
        return {"sent": False, "channel": "shadow_mode_suppressed", "detail": ""}
    sender = TASK_CHANNEL_SENDERS.get(channel)
    if sender is None:
        log(f"Employee {employee_number} has unrecognized channel {channel!r} — skipping send", level="warning")
        return {"sent": False, "channel": "none", "detail": f"unrecognized channel {channel!r}"}
    to_context = {"phone": employee.get("phone"), "fcm_token": employee.get("fcm_token")}
    result = sender.send(to_context, message)
    return result.to_dict()


async def _notify_assignee(task_id: str):
    """After a fresh assignment (or reassignment), sends the assignment
    SMS and records the outcome via mark_notified()/mark_notify_failed()
    — a visible state either way, per effort_engine.py's
    WORKFLOW_STATUSES docstring: an assignment that couldn't be
    delivered must be VISIBLE (notify_failed), never silently dropped."""
    t = effort_engine.tasks.get(task_id)
    if t is None or not t.assigned_to:
        return
    message = (
        f'Floorwatch: you are assigned "{t.task_name}" — {t.assigned_minutes:.0f} min allocated. '
        f"Reply START to begin, DONE when finished, MORE if you need extra time, "
        f"or REVIEW to ask a supervisor to check in. Task code: {effort_engine.short_code(task_id)}."
    )
    result = await asyncio.to_thread(_send_task_notification, t.assigned_to, message)
    if result.get("sent") or result.get("channel") == "shadow_mode_suppressed":
        follow = effort_engine.mark_notified(task_id)
    else:
        follow = effort_engine.mark_notify_failed(task_id, reason=result.get("detail", ""))
    if follow:
        await _emit(follow)


# ── Auto-assignment — the CCTV-driven half of the task workflow (spec
# flow 1: "task should be assigned automatically from CCTV monitor").
# Only ever invoked from tick_loop(), which only runs on the leader, so
# this calls _handle_assign_task directly rather than via the command
# bus — same reasoning as engine.tick()/effort_engine.tick() events
# being emitted directly above.
#
# Targets the department's PRIMARY-CONTACT SUPERVISOR by default, not a
# line employee directly — see PHASE_2_NOTES.md's Feature 2 section for
# why this is consistent with Global Constraint 2 (anonymity of the
# DETECTED person is untouched; only a supervisor's already-nameable
# identity is used). The supervisor then delegates to whoever should
# actually do the work — see _handle_reassign_task /
# _handle_sms_reassign below. _least_loaded_employee() is kept as the
# suggestion a supervisor's delegation UI can offer (GET
# /api/admin/employees/suggest), not as an automatic fallback. ─────────

def _primary_contact_for_department(department: str) -> str | None:
    """The one active supervisor flagged is_primary_contact for this
    department. If more than one is misconfigured as primary for the
    same department, picks deterministically (lowest employee_number)
    and logs a warning rather than picking arbitrarily/crashing —
    add_employee() is supposed to prevent this by role validation, but
    two DIFFERENT supervisors can each still legitimately be flagged
    primary for the same department through no single invalid write."""
    candidates = [e for e in employee_directory.list_all(department=department, active_only=True)
                  if e.get("role") == "supervisor" and e.get("is_primary_contact")]
    if not candidates:
        return None
    candidates.sort(key=lambda e: e["employee_number"])
    if len(candidates) > 1:
        log(f"department '{department}' has {len(candidates)} active primary contacts "
            f"({[e['employee_number'] for e in candidates]}) — using the lowest employee_number; "
            f"this should be cleaned up so only one is flagged.", level="warning")
    return candidates[0]["employee_number"]


def _least_loaded_employee(department: str) -> str | None:
    """Picks the department's active employee with the fewest currently
    OPEN tasks assigned to them (ties broken by employee_number for
    determinism). Only considers role == "employee". No longer called
    automatically by _maybe_auto_assign (see module comment above) —
    exposed via GET /api/admin/employees/suggest as a delegation
    suggestion for the primary-contact supervisor who's deciding who
    should actually do the work."""
    candidates = [e for e in employee_directory.list_all(department=department, active_only=True)
                  if e.get("role") == "employee"]
    if not candidates:
        return None
    load: dict = {}
    for t in effort_engine.tasks.values():
        if t.status == "open" and t.assigned_to:
            load[t.assigned_to] = load.get(t.assigned_to, 0) + 1
    candidates.sort(key=lambda e: (load.get(e["employee_number"], 0), e["employee_number"]))
    return candidates[0]["employee_number"]


def _auto_task_already_open_for_zone(zone_id: str) -> bool:
    """Dedup guard — a zone left unresolved keeps producing zone_escalated
    on every subsequent tick until a supervisor acts; without this, each
    tick would spawn a new coverage task for the same gap."""
    return any(t.status == "open" and t.zone_id == zone_id and t.task_type == "auto_coverage"
               for t in effort_engine.tasks.values())


async def _maybe_auto_assign(evt: dict):
    event_type = evt.get("event_type")
    if event_type not in config.AUTO_ASSIGN_TRIGGER_EVENT_TYPES:
        return
    zone_id = evt.get("zone_id")
    if not zone_id or _auto_task_already_open_for_zone(zone_id):
        return
    department = evt.get("role_tag", "")
    assignee = _primary_contact_for_department(department)
    payload = {
        "task_name": f"Cover {effort_engine._zone_label(zone_id)}", "zone_id": zone_id,
        "assigned_minutes": config.AUTO_ASSIGN_DEFAULT_MINUTES, "task_type": "auto_coverage",
        "assigned_to": assignee, "assigned_by": "system:auto_assign",
    }
    result = await _handle_assign_task(payload)
    if result.get("error"):
        log(f"auto-assign could not create a coverage task for zone '{zone_id}': {result['error']}",
            level="warning")
    elif assignee is None:
        # Created, but unassigned — surfaces in /api/tasks with
        # workflow_status "unassigned" so a supervisor can hand-assign
        # it; per the spec's own fallback requirement (no eligible
        # primary contact should never mean the gap silently goes
        # untracked).
        log(f"auto-assign created an unassigned coverage task for zone '{zone_id}' — "
            f"no active primary-contact supervisor configured for department '{department}'.",
            level="warning")
    else:
        task_id = result["event"]["task_id"]
        auto_evt = effort_engine.auto_assigned_event(task_id)
        if auto_evt:
            await _emit(auto_evt)


# ── Command bus dispatch table — see cluster_bus.py's module docstring.
# Every mutating REST endpoint (AND the SMS webhook — Twilio can hit
# ANY replica, and only the leader's effort_engine.tasks is authoritative)
# submits one of these instead of calling engine/effort_engine directly;
# only the leader's consume_commands_loop ever actually calls them. ──────

async def _handle_approve_zone(payload: dict) -> dict:
    evt = engine.approve(payload["zone_id"], supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown zone or nothing pending"}
    await _emit(evt)
    return {"event": evt}


async def _handle_reassign_zone(payload: dict) -> dict:
    evt = engine.reassign(payload["zone_id"], supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown zone"}
    await _emit(evt)
    return {"event": evt}


async def _handle_assign_task(payload: dict) -> dict:
    evt = effort_engine.assign_task(payload["task_name"], payload["zone_id"],
                                     payload["assigned_minutes"], payload.get("task_type"),
                                     assigned_to=payload.get("assigned_to"),
                                     assigned_by=payload.get("assigned_by"))
    if evt is None:
        return {"error": f"zone '{payload['zone_id']}' is not staffed per the roster — task not created"}
    await _emit(evt)
    if evt.get("assigned_to"):
        await _notify_assignee(evt["task_id"])
    return {"event": evt}


async def _handle_extend_task(payload: dict) -> dict:
    evt = effort_engine.extend_task(payload["task_id"], payload["extra_minutes"],
                                     supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown task, task not open, or extra_minutes must be positive"}
    await _emit(evt)
    return {"event": evt}


async def _handle_reassign_task(payload: dict) -> dict:
    """Reassigns the ASSIGNEE (who's doing the task) — distinct from
    reassign_zone above, which is about coverage, not this workflow.

    new_assignee may be ANY employee in the directory — supervisor or
    line staff, not restricted — per the spec's delegation requirement:
    the primary-contact supervisor an auto-assigned task lands on must
    be able to hand it to whoever should actually do the work. Only
    existence/active-status is validated here; role is deliberately
    unchecked."""
    new_employee = employee_directory.get(payload["new_assignee"])
    if new_employee is None or not new_employee.get("active", True):
        return {"error": f"employee '{payload['new_assignee']}' not found or inactive"}
    evt = effort_engine.reassign_task(payload["task_id"], payload["new_assignee"],
                                       supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown task or task not open"}
    await _emit(evt)
    await _notify_assignee(payload["task_id"])  # re-runs the notification path for the NEW assignee
    return {"event": evt}


async def _handle_sms_reply(payload: dict) -> dict:
    """Everything the inbound-SMS webhook needs resolved server-side —
    see main.py's webhook route for why this whole thing is ONE command
    rather than the webhook resolving the task itself: the webhook can
    land on any replica, but only the leader's effort_engine.tasks is
    authoritative."""
    employee = employee_directory.get_by_phone(payload["phone"])
    if employee is None:
        return {"reply": "Your number isn't recognized. Contact your supervisor to update your contact info."}

    action, code = parse_sms_command(payload["body"])
    if action is None:
        return {"reply": "Reply START, DONE, MORE, REVIEW, or REASSIGN <employee number> "
                          "(add the task code too if you have more than one open task)."}

    if action == "REASSIGN":
        return await _handle_sms_reassign(employee, code)

    task, reason = effort_engine.resolve_task_reference(employee["employee_number"], code)
    if task is None:
        return {"reply": reason}

    handler = {
        "START": effort_engine.mark_started, "DONE": effort_engine.complete_task,
        "MORE": effort_engine.request_extension, "REVIEW": effort_engine.request_review,
    }[action]
    evt = handler(task.task_id)
    if evt is None:
        return {"reply": f'Could not update "{task.task_name}" — it may have already changed status.'}
    await _emit(evt)

    reply_text = {
        "START": f'Got it — "{task.task_name}" marked in progress.',
        "DONE": f'Thanks — "{task.task_name}" marked complete.',
        "MORE": f'Noted — a supervisor will follow up about extra time on "{task.task_name}".',
        "REVIEW": f'Noted — a supervisor will review "{task.task_name}".',
    }[action]
    return {"reply": reply_text}


async def _handle_sms_reassign(sender_employee: dict, remainder: str | None) -> dict:
    """The employee-side reassignment/delegation path (Conflict 2's
    resolution) — an assignee texts "REASSIGN <employee number>"
    (optionally plus a task code if they have more than one open task)
    to hand their task off to anyone else in the directory, supervisor
    or line staff. Authenticated purely by the sender's phone number
    already having resolved to `sender_employee` above — same trust
    model as START/DONE/MORE/REVIEW, no Bearer token involved (line
    employees have no login accounts to hold one)."""
    if not remainder:
        return {"reply": "Reply REASSIGN <employee number> to hand off your task "
                          "(add the task code too if you have more than one open task)."}
    parts = remainder.split(None, 1)
    target_employee_number = parts[0]
    task_code = re.sub(r"[^A-Za-z0-9]", "", parts[1]).upper() if len(parts) > 1 else None

    target = employee_directory.get(target_employee_number)
    if target is None or not target.get("active", True):
        return {"reply": f"Employee {target_employee_number} not found or inactive — "
                          f"check the number and try again."}

    task, reason = effort_engine.resolve_task_reference(sender_employee["employee_number"], task_code)
    if task is None:
        return {"reply": reason}

    evt = effort_engine.reassign_task(task.task_id, target_employee_number,
                                       supervisor_id=f"employee:{sender_employee['employee_number']}")
    if evt is None:
        return {"reply": f'Could not reassign "{task.task_name}" — it may have already changed status.'}
    await _emit(evt)
    await _notify_assignee(task.task_id)  # sends the new assignee their own assignment message
    return {"reply": f'Done — "{task.task_name}" handed off to employee {target_employee_number}.'}


async def _handle_complete_task(payload: dict) -> dict:
    evt = effort_engine.complete_task(payload["task_id"])
    if evt is None:
        return {"error": "unknown task or already completed"}
    await _emit(evt)
    return {"event": evt}


async def _handle_confirm_flag(payload: dict) -> dict:
    """confirm_flag() reopens the task (see its docstring) rather than
    resolving it — so this must actually notify the employee, same as a
    fresh assignment/reassignment, not just log a message CLAIMING a
    follow-up happened."""
    task_id = payload["task_id"]
    evt = effort_engine.confirm_flag(task_id, supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown task or nothing pending"}
    # confirm_flag() reset the task's start_monotonic to "now" — the
    # wall-clock anchor task_store/rehydrate_tasks() uses for restart
    # recovery must be reset to match, or a restart after this point
    # would compute a wildly inflated elapsed time against the NEW
    # monotonic start using the OLD (pre-reopen) wall-clock timestamp.
    _task_started_at[task_id] = datetime.now(timezone.utc).isoformat()
    await _emit(evt)
    await _notify_assignee(task_id)  # actually sends the follow-up message this time
    return {"event": evt}


async def _handle_dismiss_flag(payload: dict) -> dict:
    evt = effort_engine.dismiss_flag(payload["task_id"], supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "unknown task or nothing pending"}
    await _emit(evt)
    return {"event": evt}


async def _handle_resolve_after_review(payload: dict) -> dict:
    """The direct exit from a confirm_flag()-reopened task — see
    resolve_after_review()'s docstring for why this exists separately
    from complete_task(): without it, a reopened task's only way back to
    the supervisor was re-running the exact check that flagged it,
    which (with no new motion signal) reflags it every time and sends
    it right back to the queue."""
    evt = effort_engine.resolve_after_review(
        payload["task_id"], supervisor_id=payload.get("supervisor_id", "supervisor"))
    if evt is None:
        return {"error": "task not open or was not reopened for review"}
    await _emit(evt)
    return {"event": evt}


COMMAND_DISPATCH = {
    "approve_zone": _handle_approve_zone,
    "reassign_zone": _handle_reassign_zone,
    "assign_task": _handle_assign_task,
    "complete_task": _handle_complete_task,
    "confirm_flag": _handle_confirm_flag,
    "dismiss_flag": _handle_dismiss_flag,
    "resolve_after_review": _handle_resolve_after_review,
    "extend_task": _handle_extend_task,
    "reassign_task": _handle_reassign_task,
    "sms_reply": _handle_sms_reply,
}


async def command_consumer_loop():
    await consume_commands_loop(cluster_redis, REPLICA_ID, COMMAND_DISPATCH)


async def redis_consumer_loop():
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        await client.xgroup_create(config.REDIS_STREAM, config.REDIS_CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            log(f"could not create consumer group: {e}", level="warning")

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
                    await _emit(evt)
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
            log(f"could not create motion consumer group: {e}", level="warning")

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
            await _emit(evt)
            await _maybe_auto_assign(evt)
        for evt in effort_engine.tick():
            await _emit(evt)
            if evt.get("event_type") == "task_status_nudge":
                # the event's own message already has the full text —
                # reuse it verbatim rather than building it twice.
                await asyncio.to_thread(_send_task_notification, evt.get("assigned_to"), evt.get("message", ""))
        # active_seconds/elapsed_minutes keep changing even on ticks that
        # produce no events — refresh regardless so /api/tasks doesn't
        # go stale between events.
        await _refresh_snapshots()


leadership = LeaderElection(cluster_redis, owner_id=REPLICA_ID)
_leader_tasks: list = []


async def _start_leader_tasks():
    """Only the current leader ever runs the stream/tick/command
    processing — see leader_election.py's module docstring for why
    (duplicate real notifications, inconsistent partial state)."""
    global _leader_tasks
    log(f"Acquired rules-engine leadership (replica={REPLICA_ID}) — "
        f"starting stream/tick/command processing.")

    # Restores open tasks from the durable store — a restart used to
    # silently lose every in-flight task; this is what fixes that. Only
    # done here (leadership acquisition), never redundantly per-request —
    # effort_engine.tasks is only ever authoritative on whichever replica
    # currently holds leadership.
    restored = await asyncio.to_thread(rehydrate_tasks, task_store, effort_engine, effort_engine._clock)
    if restored:
        for rec in await asyncio.to_thread(task_store.list_open):
            if rec["task_id"] in effort_engine.tasks:
                _task_started_at[rec["task_id"]] = rec["started_at"]

    _leader_tasks = [
        asyncio.create_task(redis_consumer_loop()),
        asyncio.create_task(motion_consumer_loop()),
        asyncio.create_task(tick_loop()),
        asyncio.create_task(command_consumer_loop()),
    ]


async def _stop_leader_tasks():
    global _leader_tasks
    if _leader_tasks:
        log(f"Lost rules-engine leadership (replica={REPLICA_ID}) — "
            f"stopping stream/tick/command processing.")
    for t in _leader_tasks:
        t.cancel()
    _leader_tasks = []


async def _on_leadership_change(is_leader: bool):
    if is_leader:
        await _start_leader_tasks()
    else:
        await _stop_leader_tasks()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Establish INITIAL leadership synchronously, before yield — so a
    # single-instance deployment (the common case, and every existing
    # test) has its stream/tick/command tasks already running by the
    # time startup completes, with no acquisition-race window. The
    # leadership_loop background task below only fires _on_leadership_change
    # for actual TRANSITIONS after this point (renewal, or a real failover).
    if await leadership.try_acquire_or_renew():
        await _start_leader_tasks()
    else:
        log(f"Another replica already holds rules-engine leadership — "
            f"this replica ({REPLICA_ID}) starts as a follower.")

    # Always runs, on every replica, leader or not — this is what lets a
    # dashboard connected to a follower see events the leader produces.
    broadcast_task = asyncio.create_task(
        broadcast_subscriber_loop(cluster_redis, REPLICA_ID, manager.broadcast))
    leadership_task = asyncio.create_task(leadership_loop(leadership, _on_leadership_change))

    log(f"Rules engine started. shadow_mode={config.SHADOW_MODE} replica_id={REPLICA_ID} "
        f"leader={leadership.is_leader}")
    yield
    leadership_task.cancel()
    broadcast_task.cancel()
    await _stop_leader_tasks()
    await leadership.release()
    await cluster_redis.aclose()


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
    return {"ok": True, "shadow_mode": config.SHADOW_MODE, "connections": len(manager.active),
            "replica_id": REPLICA_ID, "is_leader": leadership.is_leader}


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


# ── Employee directory (floor staff a task can be assigned to) ──────────
# require_supervisor, not require_admin — supervisors need to be able to
# add their own department's people (flow 3 in the workflow spec), not
# just admins. No leader/command-bus routing needed here: unlike
# engine/effort_engine, employee_directory has no in-memory-only state —
# every replica reads/writes the same Postgres/JSON store directly, same
# as floorwatch_users already does.

class AddEmployeeRequest(BaseModel):
    employee_number: str
    name: str
    role: str  # "employee" | "supervisor"
    department: str
    phone: str
    account_username: str | None = None  # links to a floorwatch_users login, if the person has one
    channel: str | None = None  # "sms" | "fcm" — Feature 1; omit to use the deployment default
    fcm_token: str | None = None  # required if channel="fcm"
    is_primary_contact: bool = False  # Feature 2 — must be a "supervisor"; see validate_primary_contact


@app.get("/api/admin/employees")
async def list_employees(department: str | None = None, user=Depends(require_supervisor)):
    return {"employees": employee_directory.list_all(department=department)}


@app.get("/api/admin/employees/suggest")
async def suggest_employee(department: str, user=Depends(require_supervisor)):
    """The least-loaded-employee logic that used to run auto-assignment
    directly, before Feature 2 — now a suggestion a primary-contact
    supervisor's delegation UI can offer when reassigning an
    auto-assigned task, not something that runs unattended."""
    suggestion = await asyncio.to_thread(_least_loaded_employee, department)
    return {"suggested_employee_number": suggestion}


@app.post("/api/admin/employees")
async def add_employee(body: AddEmployeeRequest, user=Depends(require_supervisor)):
    ok, reason = validate_employee_number(body.employee_number)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    ok, reason = validate_phone(body.phone)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    if body.role not in ("employee", "supervisor"):
        return JSONResponse(status_code=400, content={"error": "role must be 'employee' or 'supervisor'"})
    if not body.department.strip():
        return JSONResponse(status_code=400, content={"error": "department is required"})
    ok, reason = validate_channel(body.channel)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    if body.channel == "fcm" and not body.fcm_token:
        return JSONResponse(status_code=400, content={
            "error": "fcm_token is required when channel is 'fcm'"})
    ok, reason = validate_primary_contact(body.role, body.is_primary_contact)
    if not ok:
        return JSONResponse(status_code=400, content={"error": reason})
    await asyncio.to_thread(
        employee_directory.add, body.employee_number, body.name, body.role, body.department,
        body.phone, account_username=body.account_username, created_by=user["sub"],
        channel=body.channel, fcm_token=body.fcm_token, is_primary_contact=body.is_primary_contact)
    log(f"'{user['sub']}' added directory entry for employee {body.employee_number} "
        f"({body.role}, {body.department}"
        f"{', primary contact' if body.is_primary_contact else ''})")
    return {"ok": True}


@app.post("/api/admin/employees/{employee_number}/deactivate")
async def deactivate_employee(employee_number: str, user=Depends(require_supervisor)):
    if not await asyncio.to_thread(employee_directory.set_active, employee_number, False):
        return JSONResponse(status_code=404, content={"error": f"employee '{employee_number}' not found"})
    return {"ok": True}


@app.post("/api/admin/employees/{employee_number}/reactivate")
async def reactivate_employee(employee_number: str, user=Depends(require_supervisor)):
    if not await asyncio.to_thread(employee_directory.set_active, employee_number, True):
        return JSONResponse(status_code=404, content={"error": f"employee '{employee_number}' not found"})
    return {"ok": True}


# ── Inbound SMS webhook (Twilio) — the employee-reply half of the task
# workflow. NOT behind require_auth: Twilio has no bearer token to send.
# X-Twilio-Signature is the ONLY authentication here — see
# sms_webhook.py's module docstring for why that check is not optional. ──

@app.post("/api/webhooks/twilio-sms")
async def twilio_sms_webhook(request: Request):
    if not config.PUBLIC_BASE_URL:
        log("Inbound SMS webhook called but FLOORWATCH_PUBLIC_BASE_URL isn't set — "
            "cannot validate the request signature, refusing.", level="warning")
        return JSONResponse(status_code=503, content={"error": "webhook not configured"})

    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    url = config.PUBLIC_BASE_URL.rstrip("/") + request.url.path

    if not validate_signature(config.TWILIO_AUTH_TOKEN, url, params, signature):
        log("Inbound SMS webhook: signature validation failed — rejecting "
            "(this is either a misconfiguration or a forged request).", level="warning")
        return JSONResponse(status_code=403, content={"error": "invalid signature"})

    from_number = params.get("From", "")
    body_text = params.get("Body", "")
    reply = await submit_command(cluster_redis, "sms_reply", {"phone": from_number, "body": body_text})
    if reply.get("__timeout__"):
        reply_text = "Floorwatch is temporarily unavailable — please try again shortly."
    else:
        reply_text = reply.get("reply", "Sorry, something went wrong processing that.")

    from fastapi import Response
    return Response(content=reply_twiml(reply_text), media_type="application/xml")


def _command_reply_to_response(reply: dict, error_status: int = 404):
    """Every mutating endpoint below turns a command-bus reply into an
    HTTP response the same way — a timeout means no replica currently
    holds leadership (e.g. mid-failover), which is a 503, not a 404/400
    (the request wasn't invalid, the system just couldn't process it
    right now)."""
    if reply.get("__timeout__"):
        return JSONResponse(status_code=503, content={
            "error": "No rules-engine leader available to process this action right now — try again shortly."})
    if "error" in reply:
        return JSONResponse(status_code=error_status, content={"error": reply["error"]})
    return reply["event"]


@app.get("/api/state")
async def get_state(user=Depends(require_auth)):
    return await read_snapshot(cluster_redis, SNAPSHOT_STATE_KEY, default={})


@app.get("/api/digest")
async def get_digest(user=Depends(require_auth)):
    return digest.read_all()


@app.get("/api/history")
async def get_event_history(
    event_type: str | None = None, zone_id: str | None = None, task_id: str | None = None,
    since: str | None = None, until: str | None = None, limit: int = 200,
    user=Depends(require_auth),
):
    """Durable audit trail — the full zone/task lifecycle (assignment,
    nudges, flags, resolutions, supervisor actions), unlike /api/state
    and /api/tasks which only ever show CURRENT status. See
    event_history.py's module docstring for what's excluded and why."""
    limit = max(1, min(limit, 1000))  # bound it — query params are caller-controlled
    return await asyncio.to_thread(
        event_history.query, event_type=event_type, zone_id=zone_id, task_id=task_id,
        since=since, until=until, limit=limit)


@app.get("/api/queue")
async def get_queue(user=Depends(require_auth)):
    return await read_snapshot(cluster_redis, SNAPSHOT_QUEUE_KEY, default=[])


@app.post("/api/queue/zone/{zone_id}/approve")
async def approve_zone(zone_id: str, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "approve_zone", {"zone_id": zone_id, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply)


@app.post("/api/queue/zone/{zone_id}/reassign")
async def reassign_zone(zone_id: str, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "reassign_zone", {"zone_id": zone_id, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply)


class TaskAssignRequest(BaseModel):
    task_name: str
    zone_id: str
    assigned_minutes: float
    task_type: str | None = None
    assigned_to: str | None = None  # employee_number — omit for an unassigned task


@app.post("/api/tasks")
async def assign_task(body: TaskAssignRequest, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "assign_task", {
        "task_name": body.task_name, "zone_id": body.zone_id,
        "assigned_minutes": body.assigned_minutes, "task_type": body.task_type,
        "assigned_to": body.assigned_to,
        "assigned_by": f"user:{user['sub']}" if body.assigned_to else None,
    })
    return _command_reply_to_response(reply, error_status=400)


class TaskExtendRequest(BaseModel):
    extra_minutes: float


@app.post("/api/tasks/{task_id}/extend")
async def extend_task(task_id: str, body: TaskExtendRequest, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "extend_task", {
        "task_id": task_id, "extra_minutes": body.extra_minutes, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply, error_status=400)


class TaskReassignRequest(BaseModel):
    new_assignee: str  # employee_number


@app.post("/api/tasks/{task_id}/reassign")
async def reassign_task(task_id: str, body: TaskReassignRequest, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "reassign_task", {
        "task_id": task_id, "new_assignee": body.new_assignee, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply, error_status=400)


@app.get("/api/tasks")
async def list_tasks(user=Depends(require_auth)):
    return await read_snapshot(cluster_redis, SNAPSHOT_TASKS_KEY, default={})


@app.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: str, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "complete_task", {"task_id": task_id})
    return _command_reply_to_response(reply)


@app.get("/api/queue/tasks")
async def get_task_queue(user=Depends(require_auth)):
    return await read_snapshot(cluster_redis, SNAPSHOT_QUEUE_TASKS_KEY, default=[])


@app.post("/api/queue/task/{task_id}/confirm")
async def confirm_task_flag(task_id: str, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "confirm_flag", {"task_id": task_id, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply)


@app.post("/api/queue/task/{task_id}/dismiss")
async def dismiss_task_flag(task_id: str, user=Depends(require_supervisor)):
    reply = await submit_command(cluster_redis, "dismiss_flag", {"task_id": task_id, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply)


@app.post("/api/tasks/{task_id}/resolve-review")
async def resolve_task_after_review(task_id: str, user=Depends(require_supervisor)):
    """Closes out a task that was reopened via confirm_flag(), without
    re-running the effort-flag check — see resolve_after_review()'s
    docstring in effort_engine.py."""
    reply = await submit_command(
        cluster_redis, "resolve_after_review", {"task_id": task_id, "supervisor_id": user["sub"]})
    return _command_reply_to_response(reply, error_status=400)


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
