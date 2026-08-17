"""Cross-replica coordination primitives for the horizontal-scaling fix —
see `leader_election.py`'s module docstring for why a single leader owns
`RulesEngine`/`EffortEngine`. This module is what lets every OTHER
replica still work correctly despite not owning that state:

  - **Snapshots**: the leader writes a small JSON blob to Redis after
    every state change; every replica's read-only GET endpoints
    (`/api/state`, `/api/queue`, `/api/tasks`, `/api/queue/tasks`) read
    that blob instead of touching `engine`/`effort_engine` directly — so
    they return the same answer regardless of which replica actually
    processed the underlying event.
  - **Command bus**: every mutating REST endpoint (approve/reassign/
    assign_task/complete_task/confirm_flag/dismiss_flag) submits a
    command via Redis Streams instead of calling `engine`/`effort_engine`
    directly, and waits for the leader's reply. This is deliberately
    uniform — even a request that happens to land on the replica that
    IS currently the leader still goes through this same path, so there
    is only ever one way a mutation happens, not two behaviorally-
    -identical-in-theory code paths to keep in sync.
  - **Broadcast fan-out**: the leader appends every event it produces to
    a Redis Stream; every replica (leader included, for uniformity — see
    main.py) reads that stream under its OWN uniquely-named consumer
    group and forwards each entry to its own locally-connected WebSocket
    clients via the existing `ConnectionManager`. This is what makes two
    dashboards connected to two different replicas see the same live
    events.

    This deliberately uses a Stream with one consumer group PER REPLICA,
    not Redis Pub/Sub — two reasons. First, correctness under this
    project's own test infrastructure: `fakeredis`'s `TcpFakeServer` (the
    fake Redis every test in this suite runs against — no real
    Redis/Docker available here) does not reliably deliver Pub/Sub
    messages across separate connections; confirmed directly (a minimal
    subscribe/publish reproduction never received the message, despite
    PUBLISH correctly reporting a subscriber count), while the exact same
    reproduction using a Stream + per-consumer-group read worked
    correctly. Second, and separately worth having regardless of the
    test-infra issue: Streams give each replica at-least-once delivery —
    a replica that's briefly disconnected and reconnects resumes from
    where it left off, rather than silently missing whatever was
    published during the gap the way Pub/Sub would. A per-replica group
    reading from "$" (now) rather than the stream's start means a freshly
    -started replica doesn't replay history — it only sees NEW events
    from the moment it starts, matching live-dashboard semantics rather
    than an audit log.

All pieces reuse the same general-purpose Redis client — no new
infrastructure beyond the Redis instance this service already requires.
"""

import asyncio
import json
import uuid
from typing import Awaitable, Callable, Optional

from floorwatch_logging import get_logger

COMMAND_STREAM = "floorwatch:commands"
COMMAND_GROUP = "floorwatch-rules-engine-commands"
REPLY_KEY_PREFIX = "floorwatch:command_reply:"
REPLY_KEY_TTL_SECONDS = 30
BROADCAST_STREAM = "floorwatch:broadcast_stream"
BROADCAST_STREAM_MAXLEN = 1000  # approximate cap — see broadcast_subscriber_loop's docstring

DEFAULT_COMMAND_TIMEOUT_SECONDS = 5.0

_log = get_logger("rules-engine.cluster_bus")


# ── Snapshots ────────────────────────────────────────────────────────────

async def write_snapshot(redis_client, key: str, data) -> None:
    await redis_client.set(key, json.dumps(data))


async def read_snapshot(redis_client, key: str, default=None):
    raw = await redis_client.get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


# ── Command bus (mutating REST endpoints -> leader-only engine calls) ────

async def submit_command(redis_client, command_type: str, payload: dict,
                          timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> dict:
    """Called by ANY replica's REST handler. Publishes the command and
    blocks (without tying up the event loop — this is a real async wait,
    other requests keep being served) until the leader replies or
    `timeout_seconds` elapses. A timeout means no replica currently holds
    leadership (e.g. mid-failover) — returns {"__timeout__": True} rather
    than raising, so callers can turn it into a clean 503."""
    request_id = str(uuid.uuid4())
    await redis_client.xadd(COMMAND_STREAM, {
        "request_id": request_id, "command_type": command_type, "payload": json.dumps(payload),
    })
    reply_key = f"{REPLY_KEY_PREFIX}{request_id}"
    result = await redis_client.blpop(reply_key, timeout=timeout_seconds)
    if result is None:
        return {"__timeout__": True}
    _, raw = result
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "malformed reply from rules-engine leader"}


async def consume_commands_loop(redis_client, consumer_name: str,
                                 dispatch: dict[str, Callable[[dict], Awaitable[dict]]]):
    """Run ONLY by the current leader (main.py gates this on leadership).
    Mirrors main.py's redis_consumer_loop/motion_consumer_loop pattern —
    same xgroup_create/xreadgroup/xack shape — so it's consistent with
    the rest of this service's Redis Streams usage."""
    try:
        await redis_client.xgroup_create(COMMAND_STREAM, COMMAND_GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            _log(f"could not create command consumer group: {e}", level="warning")

    _log(f"Consuming command stream '{COMMAND_STREAM}' as group '{COMMAND_GROUP}'")
    while True:
        try:
            resp = await redis_client.xreadgroup(
                COMMAND_GROUP, consumer_name, {COMMAND_STREAM: ">"}, count=10, block=2000)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"Command stream read error: {e} — retrying in 2s")
            await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                request_id = fields.get("request_id")
                command_type = fields.get("command_type")
                try:
                    payload = json.loads(fields.get("payload") or "{}")
                except json.JSONDecodeError:
                    payload = {}

                handler = dispatch.get(command_type)
                if handler is None:
                    reply = {"error": f"unknown command_type '{command_type}'"}
                else:
                    try:
                        reply = await handler(payload)
                    except Exception as e:
                        _log(f"error handling command '{command_type}': {e}", level="error")
                        reply = {"error": "internal error processing this action"}

                if request_id:
                    reply_key = f"{REPLY_KEY_PREFIX}{request_id}"
                    await redis_client.rpush(reply_key, json.dumps(reply))
                    await redis_client.expire(reply_key, REPLY_KEY_TTL_SECONDS)
                await redis_client.xack(COMMAND_STREAM, COMMAND_GROUP, entry_id)


# ── Broadcast fan-out (leader's events -> every replica's WebSocket clients) ──

async def publish_event(redis_client, event: dict) -> None:
    await redis_client.xadd(BROADCAST_STREAM, {"data": json.dumps(event)},
                             maxlen=BROADCAST_STREAM_MAXLEN, approximate=True)


async def broadcast_subscriber_loop(redis_client, replica_id: str,
                                     on_event: Callable[[dict], Awaitable[None]]):
    """Run by EVERY replica (not leader-gated) — this is what lets a
    dashboard connected to a follower still see events the leader
    produced. `on_event` is expected to be the local ConnectionManager's
    `broadcast()` method (or equivalent).

    `replica_id` must be unique per replica (reuse the same one as this
    replica's LeaderElection.owner_id — see main.py) — each replica gets
    its OWN consumer group, so it receives a FULL, independent copy of
    every broadcast event rather than a split share of them the way
    redis_consumer_loop/motion_consumer_loop's SHARED group works. Starts
    reading from "$" (now), not the stream's start, so a freshly-started
    replica doesn't replay the entire history."""
    group = f"floorwatch-broadcast-{replica_id}"
    try:
        await redis_client.xgroup_create(BROADCAST_STREAM, group, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            _log(f"could not create broadcast consumer group: {e}", level="warning")

    _log(f"Consuming broadcast stream '{BROADCAST_STREAM}' as group '{group}'")
    while True:
        try:
            resp = await redis_client.xreadgroup(
                group, "reader", {BROADCAST_STREAM: ">"}, count=20, block=2000)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"Broadcast stream read error: {e} — retrying in 2s")
            await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                try:
                    event = json.loads(fields.get("data") or "{}")
                except json.JSONDecodeError:
                    event = None
                if event:
                    await on_event(event)
                await redis_client.xack(BROADCAST_STREAM, group, entry_id)
