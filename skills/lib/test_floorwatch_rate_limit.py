"""Unit tests for the shared per-caller rate limiter (used by both
floorwatch-intelligence's /api/chat and floorwatch-rules-engine's
/api/login — see floorwatch_rate_limit.py's module docstring)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from floorwatch_rate_limit import RateLimiter  # noqa: E402


def test_allows_up_to_the_limit():
    limiter = RateLimiter(max_per_window=3, window_seconds=60)
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is True


def test_blocks_once_over_the_limit():
    limiter = RateLimiter(max_per_window=2, window_seconds=60)
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is False


def test_callers_are_isolated():
    limiter = RateLimiter(max_per_window=1, window_seconds=60)
    assert limiter.allow("alice") is True
    assert limiter.allow("bob") is True  # bob's own budget, unaffected by alice
    assert limiter.allow("alice") is False
    assert limiter.allow("bob") is False


def test_old_hits_age_out_of_the_window():
    limiter = RateLimiter(max_per_window=1, window_seconds=60)
    t0 = 1000.0
    assert limiter.allow("alice", now=t0) is True
    assert limiter.allow("alice", now=t0 + 30) is False  # still within window
    assert limiter.allow("alice", now=t0 + 61) is True   # window has rolled past the first hit


def test_retry_after_seconds_reports_remaining_window():
    limiter = RateLimiter(max_per_window=1, window_seconds=60)
    t0 = 1000.0
    limiter.allow("alice", now=t0)
    assert limiter.retry_after_seconds("alice", now=t0 + 45) == 15.0


def test_retry_after_seconds_zero_when_no_hits_recorded():
    limiter = RateLimiter(max_per_window=5, window_seconds=60)
    assert limiter.retry_after_seconds("nobody") == 0.0
