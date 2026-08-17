"""Unit tests for cluster_bus.py — snapshots, the command RPC bus, and
broadcast fan-out — against a real fakeredis TcpFakeServer via
redis.asyncio, same rationale as test_leader_election.py."""

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("redis")

from cluster_bus import (  # noqa: E402
    broadcast_subscriber_loop, consume_commands_loop, publish_event,
    read_snapshot, submit_command, write_snapshot,
)


@pytest.fixture
def redis_client():
    import redis.asyncio as aioredis
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    client = aioredis.Redis.from_url(f"redis://127.0.0.1:{port}/0", decode_responses=True)
    yield client
    server.shutdown()


# ── snapshots ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_snapshot_returns_default_when_missing(redis_client):
    result = await read_snapshot(redis_client, "test:snapshot:missing", default={"empty": True})
    assert result == {"empty": True}


@pytest.mark.asyncio
async def test_write_then_read_snapshot_roundtrips(redis_client):
    await write_snapshot(redis_client, "test:snapshot:state", {"zone_a": {"status": "gap"}})
    result = await read_snapshot(redis_client, "test:snapshot:state")
    assert result == {"zone_a": {"status": "gap"}}


@pytest.mark.asyncio
async def test_write_snapshot_overwrites_previous_value(redis_client):
    await write_snapshot(redis_client, "test:snapshot:state", {"v": 1})
    await write_snapshot(redis_client, "test:snapshot:state", {"v": 2})
    assert await read_snapshot(redis_client, "test:snapshot:state") == {"v": 2}


# ── command bus ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_command_times_out_with_no_consumer(redis_client):
    reply = await submit_command(redis_client, "approve_zone", {"zone_id": "lobby"}, timeout_seconds=0.5)
    assert reply == {"__timeout__": True}


@pytest.mark.asyncio
async def test_submit_command_gets_reply_from_a_running_consumer(redis_client):
    received_payloads = []

    async def handler(payload):
        received_payloads.append(payload)
        return {"event": {"event_type": "zone_resolved", "zone_id": payload["zone_id"]}}

    consumer_task = asyncio.create_task(
        consume_commands_loop(redis_client, "test-consumer-1", {"approve_zone": handler}))
    try:
        reply = await submit_command(redis_client, "approve_zone", {"zone_id": "lobby"}, timeout_seconds=5.0)
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    assert reply == {"event": {"event_type": "zone_resolved", "zone_id": "lobby"}}
    assert received_payloads == [{"zone_id": "lobby"}]


@pytest.mark.asyncio
async def test_submit_command_unknown_command_type_gets_error_reply(redis_client):
    consumer_task = asyncio.create_task(consume_commands_loop(redis_client, "test-consumer-2", {}))
    try:
        reply = await submit_command(redis_client, "nonexistent_command", {}, timeout_seconds=5.0)
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    assert "error" in reply


@pytest.mark.asyncio
async def test_submit_command_handler_exception_becomes_error_reply_not_a_crash(redis_client):
    async def broken_handler(payload):
        raise RuntimeError("boom")

    consumer_task = asyncio.create_task(
        consume_commands_loop(redis_client, "test-consumer-3", {"will_break": broken_handler}))
    try:
        reply = await submit_command(redis_client, "will_break", {}, timeout_seconds=5.0)
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    assert "error" in reply


@pytest.mark.asyncio
async def test_multiple_commands_each_get_their_own_correct_reply(redis_client):
    async def handler(payload):
        return {"event": {"echo": payload["n"]}}

    consumer_task = asyncio.create_task(
        consume_commands_loop(redis_client, "test-consumer-4", {"echo": handler}))
    try:
        replies = await asyncio.gather(*[
            submit_command(redis_client, "echo", {"n": i}, timeout_seconds=5.0) for i in range(5)
        ])
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    assert sorted(r["event"]["echo"] for r in replies) == [0, 1, 2, 3, 4]


# ── broadcast fan-out ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_subscriber_receives_published_events(redis_client):
    received = []

    async def on_event(evt):
        received.append(evt)

    sub_task = asyncio.create_task(broadcast_subscriber_loop(redis_client, on_event))
    try:
        await asyncio.sleep(0.2)  # let the subscription actually establish
        await publish_event(redis_client, {"event_type": "zone_gap", "zone_id": "lobby"})

        deadline = time.time() + 5
        while not received and time.time() < deadline:
            await asyncio.sleep(0.05)
    finally:
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass

    assert received == [{"event_type": "zone_gap", "zone_id": "lobby"}]


@pytest.mark.asyncio
async def test_broadcast_reaches_multiple_subscribers(redis_client):
    """Simulates two replicas — both subscribed, both should see the same event."""
    received_a, received_b = [], []

    async def on_event_a(evt):
        received_a.append(evt)

    async def on_event_b(evt):
        received_b.append(evt)

    task_a = asyncio.create_task(broadcast_subscriber_loop(redis_client, on_event_a))
    task_b = asyncio.create_task(broadcast_subscriber_loop(redis_client, on_event_b))
    try:
        await asyncio.sleep(0.2)
        await publish_event(redis_client, {"event_type": "zone_resolved"})

        deadline = time.time() + 5
        while (not received_a or not received_b) and time.time() < deadline:
            await asyncio.sleep(0.05)
    finally:
        for t in (task_a, task_b):
            t.cancel()
        for t in (task_a, task_b):
            try:
                await t
            except asyncio.CancelledError:
                pass

    assert received_a == [{"event_type": "zone_resolved"}]
    assert received_b == [{"event_type": "zone_resolved"}]
