"""Per-caller rate limiting — shared between floorwatch-intelligence
(originally SECURITY_REVIEW.md H2, "Unbounded /api/chat") and
floorwatch-rules-engine (DATA_PROTECTION_SECURITY_ANALYSIS.md DP-H3,
"No rate limiting on /api/login").

Moved here from floorwatch-intelligence/app/rate_limit.py rather than
copied into rules-engine as a second file — this codebase already learned
that lesson once, the hard way: env_config.py existed as two bundled
duplicate copies (skills/lib and a per-skill copy) and one of them didn't
get the RT-DETR/ONNX-format fix applied to it, silently reintroducing a
bug the other copy had already fixed. One shared source here instead.

Deliberately a plain in-process sliding-window counter, not Redis-backed.
This is a REAL, still-open limitation if either service is ever run with
multiple replicas: each replica would enforce the limit independently
against its own share of traffic, not the combined total — an attacker
spread across N replicas effectively gets N times the intended budget.
Flagged here rather than silently wrong; fixing it means moving the
counter itself into Redis (a per-caller sorted set or similar), not just
routing every replica's check through a shared store naively (that would
turn every rate-limit check into a Redis round-trip on the hot path,
worth doing deliberately rather than as an afterthought).

This is a DIFFERENT, narrower problem than floorwatch-rules-engine's
former "no horizontal scaling support" gap (WebSocket broadcast +
in-memory RulesEngine/EffortEngine state) — that one's been fixed (see
app/leader_election.py + app/cluster_bus.py there); this limiter wasn't
in scope for that fix and still needs its own.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_per_window: int, window_seconds: float = 60.0):
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        """Returns True and records a hit if under the limit; False (no
        hit recorded) if the caller is currently over it."""
        now = now if now is not None else time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_per_window:
            return False
        hits.append(now)
        return True

    def retry_after_seconds(self, key: str, now: float | None = None) -> float:
        """How long until the caller's oldest hit ages out of the window."""
        now = now if now is not None else time.monotonic()
        hits = self._hits[key]
        if not hits:
            return 0.0
        return max(0.0, self.window_seconds - (now - hits[0]))
