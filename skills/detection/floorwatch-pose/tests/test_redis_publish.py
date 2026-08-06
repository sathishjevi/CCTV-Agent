"""Confirms floorwatch-pose publishes motion events onto its Redis Stream —
same fakeredis TcpFakeServer rationale as
skills/detection/floorwatch-coverage/tests/test_redis_publish.py."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pose import make_redis_publisher  # noqa: E402

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")


@pytest.fixture
def fake_redis_server():
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


def test_publisher_delivers_motion_event(fake_redis_server):
    publish = make_redis_publisher(fake_redis_server, "floorwatch:motion")
    event = {
        "event": "pose_motion", "frame_id": 1, "camera_id": "lobby_cam_1",
        "timestamp": "2026-07-24T10:00:00Z", "motion_score": 0.42, "active": True, "mode": "fallback",
    }
    publish(event)

    consumer = redis.Redis.from_url(fake_redis_server, decode_responses=True)
    entries = consumer.xrange("floorwatch:motion", "-", "+")
    assert len(entries) == 1
    delivered = json.loads(entries[0][1]["data"])
    assert delivered == event
