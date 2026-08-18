"""End-to-end proof that two REPLICAS of this service coordinate
correctly — everything in test_admin_users.py/test_integration.py/etc.
only ever exercises a single instance, which proves single-instance
behavior is preserved but says nothing about the actual point of this
fix (production-readiness: "the WebSocket connection manager and
in-memory state assume exactly one running instance").

Simulates two replicas by executing app/main.py's top-level code TWICE,
under two distinct module names, sharing the same fake Redis — this
gives two independent `app`/`engine`/`effort_engine`/`manager`/
`cluster_redis`/`REPLICA_ID`/`leadership`, exactly like two real
containers would each build their own, without needing two actual OS
processes. Submodules main.py imports (config, engine, effort_engine,
...) are deliberately left shared/cached between the two loads — real
replicas share the same environment/config too, and none of those hold
per-replica mutable state at the module level (only main.py's own
top-level assignments do).

The lower-level leader-election mechanics (lease acquire/renew/expiry,
on-change callback firing) are already unit-tested with fast, controlled
timing in test_leader_election.py — this file focuses on what's unique
at the full-app level: that two replicas' apps actually agree on who's
leader, route mutations to the right place, and see the same state and
events regardless of which one a client happens to be talking to.
"""

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")


def _load_main_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, APP_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def two_replicas(monkeypatch, tmp_path):
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    fake_redis_url = f"redis://127.0.0.1:{port}/0"

    import config
    monkeypatch.setattr(config, "REDIS_URL", fake_redis_url)
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")

    for name in ("main", "main_replica_a", "main_replica_b", "engine", "effort_engine"):
        sys.modules.pop(name, None)

    replica_a = _load_main_module("main_replica_a")
    replica_a.users.create_user("test-supervisor", "test-password-123", role="supervisor")
    replica_b = _load_main_module("main_replica_b")

    from fastapi.testclient import TestClient
    # entering client_a's context (and thus its lifespan) fully completes
    # BEFORE client_b's starts, so replica_a deterministically wins the
    # leadership race — replica_b starts as a follower, not a race.
    with TestClient(replica_a.app) as client_a, TestClient(replica_b.app) as client_b:
        login = client_a.post("/api/login", json={"username": "test-supervisor", "password": "test-password-123"})
        token = login.json()["token"]
        client_a.headers.update({"Authorization": f"Bearer {token}"})
        client_b.headers.update({"Authorization": f"Bearer {token}"})
        yield replica_a, client_a, replica_b, client_b, fake_redis_url

    server.shutdown()


def test_exactly_one_replica_becomes_leader(two_replicas):
    replica_a, _client_a, replica_b, _client_b, _url = two_replicas
    assert replica_a.leadership.is_leader is True
    assert replica_b.leadership.is_leader is False


def test_only_the_leader_runs_stream_and_command_processing(two_replicas):
    replica_a, _client_a, replica_b, _client_b, _url = two_replicas
    assert replica_a._leader_tasks  # leader started its background tasks
    assert not replica_b._leader_tasks  # follower did not


def test_mutation_submitted_via_follower_is_actually_applied(two_replicas):
    """The core correctness property: a REST call landing on the
    follower must still result in a real, correct mutation — not a
    silent no-op, and not a mutation applied to the wrong (follower's
    own unused) engine instance."""
    replica_a, client_a, replica_b, client_b, fake_redis_url = two_replicas
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)

    with client_a.websocket_connect(f"/events?token={client_a.headers['Authorization'].split()[1]}") as ws:
        publisher.xadd(replica_a.config.REDIS_STREAM, {"data": json.dumps({
            "camera_id": "lobby_cam_1", "zone_id": "lobby", "role_tag": "usher",
            "entity_ref": None, "event_type": "zone_gap",
            "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
        })})
        ws.receive_json()  # nudge — confirms the leader (replica_a) processed it

    # Submit the approve action via replica_b — the FOLLOWER.
    resp = client_b.post("/api/queue/zone/lobby/approve")
    assert resp.status_code == 200
    assert resp.json()["event_type"] == "zone_resolved"

    # It must have actually mutated the LEADER's real engine state.
    assert replica_a.engine.zones["lobby"].status == "covered"
    # The follower's own (unused) engine must NOT have been touched —
    # proving the mutation went through the command bus to the leader,
    # not to whatever engine instance happened to handle the HTTP request.
    assert "lobby" not in replica_b.engine.zones


def test_state_is_identical_across_replicas_after_a_mutation(two_replicas):
    replica_a, client_a, replica_b, client_b, fake_redis_url = two_replicas
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)

    with client_a.websocket_connect(f"/events?token={client_a.headers['Authorization'].split()[1]}") as ws:
        publisher.xadd(replica_a.config.REDIS_STREAM, {"data": json.dumps({
            "camera_id": "lobby_cam_1", "zone_id": "boxoffice", "role_tag": "ticketing",
            "entity_ref": None, "event_type": "zone_gap",
            "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
        })})
        ws.receive_json()

    state_from_leader = client_a.get("/api/state").json()
    state_from_follower = client_b.get("/api/state").json()
    assert state_from_leader == state_from_follower
    assert state_from_follower["boxoffice"]["status"] == "nudge"


def test_websocket_on_follower_receives_event_the_leader_produced(two_replicas):
    """This is the headline fix — a dashboard connected to a replica
    that ISN'T processing the underlying Redis stream must still see
    the live event, via the broadcast fan-out."""
    replica_a, client_a, replica_b, client_b, fake_redis_url = two_replicas
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)

    token_b = client_b.headers["Authorization"].split()[1]
    with client_b.websocket_connect(f"/events?token={token_b}") as ws_b:
        publisher.xadd(replica_a.config.REDIS_STREAM, {"data": json.dumps({
            "camera_id": "lobby_cam_1", "zone_id": "concession", "role_tag": "concession",
            "entity_ref": None, "event_type": "zone_gap",
            "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
        })})
        received = ws_b.receive_json()

    assert received["event_type"] == "zone_nudge_sent"
    assert received["zone_id"] == "concession"


def test_task_assignment_via_follower_is_visible_from_leader(two_replicas):
    replica_a, client_a, replica_b, client_b, _url = two_replicas
    resp = client_b.post("/api/tasks", json={
        "task_name": "Clean Door", "zone_id": "theatre3", "assigned_minutes": 60, "task_type": "clean_door",
    })
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    tasks_from_leader = client_a.get("/api/tasks").json()
    assert task_id in tasks_from_leader
    assert tasks_from_leader[task_id]["task_name"] == "Clean Door"
    # applied to the leader's real engine, not the follower's unused one
    assert task_id in replica_a.effort_engine.tasks
    assert task_id not in replica_b.effort_engine.tasks
