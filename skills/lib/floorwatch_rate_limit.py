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

Deliberately a plain in-process sliding-window counter, not Redis-backed:
both services run as a single process each (see each one's main.py — one
event loop, in-memory state throughout), so per-process state is already
the model everywhere else in both. If either service is ever run with
multiple worker processes/replicas, this limiter would need to move to a
shared store (Redis) to stay effective per-caller across processes —
flagged here rather than silently wrong. (This is the same underlying gap
as the "no horizontal scaling support" limitation already tracked for
floorwatch-rules-engine's WebSocket broadcast — see RAILWAY_DEPLOYMENT.md.)
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
