"""
Confirms floorwatch-coverage publishes schema events onto a Redis Stream
and a downstream consumer (the rules engine, in Phase 2) can read them via
XREAD — i.e. proves task 2 of the brief's Phase 2 spec: "Stand up Redis
Streams as the event bus. Publish events from the skill; confirm delivery
with a stub consumer."

Uses fakeredis's TcpFakeServer (an in-memory Redis protocol server bound
to a real TCP socket) instead of a real Redis instance, because this dev
sandbox cannot run Docker Desktop headlessly. The code under test
(`main.make_redis_publisher`) is unmodified production code using the
real `redis` client against `redis://<host>:<port>` — the only thing
that's fake is what's listening on the other end. Real deployment already
has Redis provisioned in docker/docker-compose.yml.

Run with: python -m pytest tests/test_redis_publish.py -v
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from main import make_redis_publisher  # noqa: E402

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")


@pytest.fixture
def fake_redis_server():
    server_address = ("127.0.0.1", 0)
    server = fakeredis.TcpFakeServer(server_address, server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


def test_publisher_delivers_event_readable_by_stub_consumer(fake_redis_server):
    publish = make_redis_publisher(fake_redis_server, "floorwatch:events")

    event = {
        "event_id": "test-1234",
        "timestamp": "2026-07-24T10:00:00Z",
        "camera_id": "lobby_cam_1",
        "zone_id": "concession_a",
        "role_tag": "concession",
        "entity_ref": "track_0",
        "event_type": "zone_covered",
        "task_id": None,
        "active_minutes": None,
        "assigned_minutes": None,
        "confidence": 0.92,
        "source_model_version": "floorwatch-coverage/0.2.0",
    }
    publish(event)

    # Stub consumer — an independent client, as the rules engine would be.
    consumer = redis.Redis.from_url(fake_redis_server, decode_responses=True)
    entries = consumer.xrange("floorwatch:events", "-", "+")

    assert len(entries) == 1
    _entry_id, fields = entries[0]
    delivered = json.loads(fields["data"])
    assert delivered == event


def test_publisher_delivers_multiple_events_in_order(fake_redis_server):
    publish = make_redis_publisher(fake_redis_server, "floorwatch:events")
    for i in range(3):
        publish({"event_type": "zone_covered", "seq": i})

    consumer = redis.Redis.from_url(fake_redis_server, decode_responses=True)
    entries = consumer.xrange("floorwatch:events", "-", "+")
    seqs = [json.loads(fields["data"])["seq"] for _, fields in entries]
    assert seqs == [0, 1, 2]
