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
  - **Broadcast fan-out**: the leader publishes every event it produces
    to a Redis Pub/Sub channel; every replica (leader included, for
    uniformity — see main.py) subscribes and forwards to its own
    locally-connected WebSocket clients via the existing
    `ConnectionManager`. This is what makes two dashboards connected to
    two different replicas see the same live events.

All three pieces reuse the same general-purpose Redis client — no new
infrastructure beyond the Redis instance this service already requires.
"""

import asyncio
import json
import sys
import uuid
from typing import Awaitable, Callable, Optional

COMMAND_STREAM = "floorwatch:commands"
COMMAND_GROUP = "floorwatch-rules-engine-commands"
REPLY_KEY_PREFIX = "floorwatch:command_reply:"
REPLY_KEY_TTL_SECONDS = 30
BROADCAST_CHANNEL = "floorwatch:broadcast"

DEFAULT_COMMAND_TIMEOUT_SECONDS = 5.0


def _log(msg: str):
    print(f"[cluster_bus] {msg}", file=sys.stderr, flush=True)


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
            _log(f"WARNING: could not create command consumer group: {e}")

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
                        _log(f"ERROR handling command '{command_type}': {e}")
                        reply = {"error": "internal error processing this action"}

                if request_id:
                    reply_key = f"{REPLY_KEY_PREFIX}{request_id}"
                    await redis_client.rpush(reply_key, json.dumps(reply))
                    await redis_client.expire(reply_key, REPLY_KEY_TTL_SECONDS)
                await redis_client.xack(COMMAND_STREAM, COMMAND_GROUP, entry_id)


# ── Broadcast fan-out (leader's events -> every replica's WebSocket clients) ──

async def publish_event(redis_client, event: dict) -> None:
    await redis_client.publish(BROADCAST_CHANNEL, json.dumps(event))


async def broadcast_subscriber_loop(redis_client, on_event: Callable[[dict], Awaitable[None]]):
    """Run by EVERY replica (not leader-gated) — this is what lets a
    dashboard connected to a follower still see events the leader
    produced. `on_event` is expected to be the local ConnectionManager's
    `broadcast()` method (or equivalent)."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(BROADCAST_CHANNEL)
    _log(f"Subscribed to broadcast channel '{BROADCAST_CHANNEL}'")
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                event = json.loads(message["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            await on_event(event)
    finally:
        await pubsub.unsubscribe(BROADCAST_CHANNEL)
        await pubsub.aclose()
