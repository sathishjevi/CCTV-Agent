"""Security-event audit logging and attack alerting, shared by both services.

The checklist asks for two things that were missing:
  * authentication events, authorization failures and sensitive data access
    in the logs — never passwords, tokens or other secrets;
  * real-time alerting on repeated failed logins and privilege-escalation
    attempts.

Every event is one structured log line (`security_event=<kind>`). Failures
are warnings; successes and admin actions are info. When the same kind of
event keeps arriving for the same key (an IP, a username, a login) the
monitor raises ONE error-level log per window — which floorwatch_logging
forwards to error monitoring (PostHog), where an alert rule can email or
message someone. One alert per key per window, not one per attempt.

Keys are passed in by the caller. Anything that leaves our infrastructure in
an alert message (it goes to the monitoring vendor) uses a truncated IP; the
local log line keeps the full address.
"""

import time
from collections import defaultdict, deque
from typing import Callable, Iterable, Optional

from floorwatch_request_guard import truncate_ip

# kind -> (events before an alert, within this many seconds)
DEFAULT_THRESHOLDS = {
    "login_failed": (10, 300),
    "login_rate_limited": (5, 300),
    "mfa_failed": (6, 300),
    "authz_denied": (15, 300),           # 403: a logged-in caller reaching for something above their role
    "auth_invalid_token": (200, 300),    # high: an expired token on a polling phone repeats every minute
    "request_rate_limited": (20, 300),
}
NO_COUNT = {"login_succeeded", "admin_action", "data_export", "data_erasure", "mfa_enrolled",
            "mfa_disabled", "session_revoked", "privacy_request"}
_MAX_TRACKED_KEYS = 20_000


class SecurityMonitor:
    def __init__(self, log: Callable[..., None], thresholds: Optional[dict] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._log = log
        self._thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
        self._clock = clock
        self._hits: dict = defaultdict(deque)
        self._alerted_at: dict = {}
        self.counts: dict = defaultdict(int)   # lifetime counters, for the diagnostics view

    def record(self, kind: str, /, keys: Iterable[str] = (), **fields):
        """`keys` are the things to count this event against, e.g.
        ["ip:203.0.113.9", "user:alice"]. `fields` are logged as-is, so the
        caller must not pass secrets."""
        self.counts[kind] += 1
        level = "info" if kind in NO_COUNT else "warning"
        try:
            self._log(f"security event: {kind}", level=level, security_event=kind, **fields)
        except Exception:
            pass
        threshold = self._thresholds.get(kind)
        if threshold is None or kind in NO_COUNT:
            return
        limit, window = threshold
        now = self._clock()
        for key in keys:
            self._count(kind, key, limit, window, now)

    def _count(self, kind, key, limit, window, now):
        if len(self._hits) > _MAX_TRACKED_KEYS:      # bound memory under a key-spraying attack
            self._hits.clear()
            self._alerted_at.clear()
        hits = self._hits[(kind, key)]
        hits.append(now)
        cutoff = now - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) < limit:
            return
        last = self._alerted_at.get((kind, key))
        if last is not None and now - last < window:
            return
        self._alerted_at[(kind, key)] = now
        self.counts["alerts_raised"] += 1
        try:
            # error level on purpose: this is what reaches error monitoring.
            self._log(f"possible attack: {len(hits)} '{kind}' events for {_describe(key)} "
                      f"within {int(window)}s", level="error", security_event="alert", alert_kind=kind)
        except Exception:
            pass


def _describe(key: str) -> str:
    """Make a counting key safe to send to a third-party monitoring vendor."""
    if key.startswith("ip:"):
        return f"ip {truncate_ip(key[3:])}"
    if key.startswith("phone:"):
        return "a phone login"            # never ship a phone number off-platform
    return key
