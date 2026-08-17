"""Unit tests for leader_election.py — against a real fakeredis
TcpFakeServer (same rationale as the rest of this test suite: no Docker
in this sandbox), using the actual redis.asyncio client so these tests
exercise the real SET NX/EXPIRE/GET/DELETE semantics the module depends
on, not a mock of them."""

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

from leader_election import LeaderElection, leadership_loop  # noqa: E402


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


@pytest.mark.asyncio
async def test_first_acquire_succeeds(redis_client):
    election = LeaderElection(redis_client, key="test:leader")
    assert await election.try_acquire_or_renew() is True
    assert election.is_leader is True


@pytest.mark.asyncio
async def test_second_replica_cannot_acquire_while_lease_held(redis_client):
    leader = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    follower = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    assert await leader.try_acquire_or_renew() is True
    assert await follower.try_acquire_or_renew() is False
    assert follower.is_leader is False


@pytest.mark.asyncio
async def test_renew_extends_the_lease_for_the_current_leader(redis_client):
    election = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    await election.try_acquire_or_renew()
    ttl_before = await redis_client.ttl("test:leader")
    # simulate time passing without actually sleeping 30s
    await redis_client.expire("test:leader", 5)
    assert await election.try_acquire_or_renew() is True  # renews
    ttl_after = await redis_client.ttl("test:leader")
    assert ttl_after > 5
    assert ttl_before > 0


@pytest.mark.asyncio
async def test_follower_takes_over_once_lease_expires(redis_client):
    leader = LeaderElection(redis_client, key="test:leader", lease_seconds=1)
    follower = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    await leader.try_acquire_or_renew()
    assert await follower.try_acquire_or_renew() is False

    await asyncio.sleep(1.2)  # let the 1s lease actually expire

    assert await follower.try_acquire_or_renew() is True
    assert follower.is_leader is True
    # the old leader, if it tried to renew now, must discover it lost leadership
    assert await leader.try_acquire_or_renew() is False
    assert leader.is_leader is False


@pytest.mark.asyncio
async def test_release_clears_the_lease_for_the_next_acquirer(redis_client):
    leader = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    follower = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    await leader.try_acquire_or_renew()
    await leader.release()
    assert leader.is_leader is False
    assert await redis_client.get("test:leader") is None
    assert await follower.try_acquire_or_renew() is True


@pytest.mark.asyncio
async def test_release_does_not_clobber_a_lease_taken_over_by_someone_else(redis_client):
    """If this replica's lease already expired and someone else acquired
    it, release() must not delete THEIR lease."""
    leader = LeaderElection(redis_client, key="test:leader", lease_seconds=1)
    await leader.try_acquire_or_renew()
    await asyncio.sleep(1.2)

    new_leader = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
    await new_leader.try_acquire_or_renew()

    await leader.release()  # stale — should be a no-op against new_leader's lease
    assert await redis_client.get("test:leader") == new_leader.owner_id


@pytest.mark.asyncio
async def test_leadership_loop_fires_on_change_only_on_transitions(redis_client):
    election = LeaderElection(redis_client, key="test:leader", lease_seconds=1)
    await election.try_acquire_or_renew()  # already leader before the loop starts

    changes = []

    async def on_change(is_leader):
        changes.append(is_leader)

    task = asyncio.create_task(leadership_loop(election, on_change, renew_interval_seconds=0.3))
    try:
        await asyncio.sleep(0.7)  # a couple of renewals — should stay leader, no callback yet
        assert changes == []

        # steal the lease out from under it to force a loss
        await redis_client.delete("test:leader")
        other = LeaderElection(redis_client, key="test:leader", lease_seconds=30)
        await other.try_acquire_or_renew()

        await asyncio.sleep(0.5)
        assert changes == [False]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
