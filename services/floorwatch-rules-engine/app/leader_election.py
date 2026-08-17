"""Redis-backed leader election — the core piece of the horizontal-scaling
fix (production-readiness gap: "no horizontal scaling support — the
WebSocket connection manager and in-memory state assume exactly one
running instance").

Why this exists at all: `RulesEngine`/`EffortEngine` are stateful,
in-memory, single-process state machines (zone escalation timers,
per-task active-time accrual). If two replicas both independently
consumed the same Redis Streams events, each would build its OWN
incomplete/inconsistent view (a consumer GROUP splits entries ACROSS
consumers, it doesn't duplicate them), and — more dangerously — a
real notification (Twilio SMS / FCM push) could fire twice for the same
logical event, since `notifications.py` has no idempotency/dedup
mechanism at all. The fix isn't "run the engine on every replica" (that
would need to de-duplicate side effects, which is its own hard problem)
— it's "run the engine on exactly ONE replica at a time, with automatic
failover if that replica dies." That's what this module provides.

Design: a single Redis key (`floorwatch:leader`) holds the current
leader's random owner_id, with a TTL (the lease). Whoever successfully
`SET`s it with `NX` becomes leader; the leader renews (extends the TTL)
periodically as long as it still holds the key. If the leader stops
renewing (crash, network partition, graceful shutdown), the lease
expires and another replica's next acquire attempt succeeds.

This is deliberately a simple, single-Redis-instance lease, not a
Redlock-style multi-node consensus protocol — this system already has
exactly one Redis instance as a hard dependency (the event bus), so
there's no multi-node split-brain scenario to protect against here, only
"whoever holds a live lease is leader." Momentary dual-leadership during
a handoff window is a known, accepted characteristic of lease-based
election in general — the renewal cadence (renew well before the lease
expires) keeps that window small, not zero.
"""

import asyncio
import uuid
from typing import Awaitable, Callable, Optional

from floorwatch_logging import get_logger

DEFAULT_LEASE_SECONDS = 15.0
DEFAULT_RENEW_INTERVAL_SECONDS = 5.0

_log = get_logger("rules-engine.leader_election")


class LeaderElection:
    """One instance per replica. `owner_id` must be unique per replica —
    defaults to a random UUID, which is sufficient (no need for it to be
    human-meaningful; it's only ever compared for equality)."""

    def __init__(self, redis_client, key: str = "floorwatch:leader",
                 owner_id: Optional[str] = None, lease_seconds: float = DEFAULT_LEASE_SECONDS):
        self._redis = redis_client
        self._key = key
        self.owner_id = owner_id or str(uuid.uuid4())
        self._lease_seconds = lease_seconds
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def try_acquire_or_renew(self) -> bool:
        """Call periodically. If this replica already holds the lease,
        extends it (but only after confirming it still actually owns the
        key — a previous lease could have expired and been claimed by
        another replica between calls). If not currently leader, attempts
        to acquire an unclaimed lease. Returns the resulting leadership
        state (also updates `.is_leader`)."""
        if self._is_leader:
            current = await self._redis.get(self._key)
            if current == self.owner_id:
                await self._redis.expire(self._key, int(self._lease_seconds))
                return True
            self._is_leader = False
            return False

        acquired = await self._redis.set(self._key, self.owner_id, nx=True, ex=int(self._lease_seconds))
        self._is_leader = bool(acquired)
        return self._is_leader

    async def release(self):
        """Best-effort — only deletes the key if this replica still owns
        it (never clobbers a lease some other replica may have since
        acquired). Call on graceful shutdown so the next leader doesn't
        have to wait out the full lease TTL."""
        if not self._is_leader:
            return
        current = await self._redis.get(self._key)
        if current == self.owner_id:
            await self._redis.delete(self._key)
        self._is_leader = False


async def leadership_loop(election: LeaderElection, on_change: Callable[[bool], Awaitable[None]],
                           renew_interval_seconds: float = DEFAULT_RENEW_INTERVAL_SECONDS):
    """Background task: periodically renews/attempts the lease, and calls
    `on_change(is_leader)` only when leadership actually FLIPS — not on
    every renewal. Callers that need to establish INITIAL leadership
    synchronously (so a single-instance deployment's background workers
    are already running by the time startup completes, with no race
    window) should call `election.try_acquire_or_renew()` once directly
    before starting this loop, rather than waiting for this loop's first
    iteration — this loop picks up from whatever state already exists."""
    previous = election.is_leader
    while True:
        await asyncio.sleep(renew_interval_seconds)
        try:
            current = await election.try_acquire_or_renew()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"leadership check failed ({e}) — assuming leadership lost until it recovers", level="warning")
            current = False
            election._is_leader = False
        if current != previous:
            _log(f"Leadership {'ACQUIRED' if current else 'LOST'} (owner_id={election.owner_id})")
            await on_change(current)
            previous = current
