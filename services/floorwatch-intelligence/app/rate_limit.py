"""Per-caller rate limiting — SECURITY_REVIEW.md H2 ("Unbounded /api/chat —
cost and data-exposure surface"). Auth (fixed earlier) closed the "anyone
reachable, no credentials" half of that finding; this closes the
remaining "a logged-in-but-malicious or compromised account could still
drive unbounded Anthropic API spend" half.

Deliberately a plain in-process sliding-window counter, not Redis-backed:
this service runs as a single process (see main.py's lifespan — one
ingest loop, one in-memory vector store instance), so per-process state is
already the model everywhere else here. If this service is ever run with
multiple worker processes, this limiter would need to move to a shared
store (Redis) to stay effective — flagged here rather than silently wrong.
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
