"""Client-IP extraction behind a proxy, the global rate limit, the
memory-bounded rate limiter, and security-event alerting."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_auth as auth  # noqa: E402
from floorwatch_rate_limit import RateLimiter  # noqa: E402
from floorwatch_request_guard import GlobalRateLimitMiddleware, client_ip_from_headers, truncate_ip  # noqa: E402
from floorwatch_security_events import SecurityMonitor  # noqa: E402

SECRET = "request-guard-test-secret-32-bytes-min"


# ── client IP ───────────────────────────────────────────────────────────────

def test_a_spoofed_leftmost_forwarded_for_is_ignored():
    # attacker sends "X-Forwarded-For: 6.6.6.6"; the proxy appends the address it actually saw
    headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}
    assert client_ip_from_headers(headers, "10.0.0.1", trusted_hops=1) == "203.0.113.9"


def test_rotating_fake_addresses_all_collapse_to_the_real_client():
    ips = {client_ip_from_headers({"x-forwarded-for": f"1.1.1.{i}, 203.0.113.9"}, "10.0.0.1") for i in range(50)}
    assert ips == {"203.0.113.9"}


def test_two_proxies_in_front_use_the_second_from_the_right():
    headers = {"x-forwarded-for": "6.6.6.6, 198.51.100.7, 172.16.0.2"}   # client, CDN-seen client, CDN
    assert client_ip_from_headers(headers, "10.0.0.1", trusted_hops=2) == "198.51.100.7"


def test_a_single_value_header_is_that_value():
    assert client_ip_from_headers({"x-forwarded-for": "203.0.113.9"}, "10.0.0.1") == "203.0.113.9"


def test_no_header_falls_back_to_the_direct_peer():
    assert client_ip_from_headers({}, "192.0.2.5") == "192.0.2.5"
    assert client_ip_from_headers({}, None) == "unknown"


def test_zero_trusted_hops_never_believes_the_header():
    assert client_ip_from_headers({"x-forwarded-for": "6.6.6.6"}, "192.0.2.5", trusted_hops=0) == "192.0.2.5"


def test_ips_are_truncated_before_leaving_the_platform():
    assert truncate_ip("203.0.113.9") == "203.0.113.0/24"
    assert truncate_ip("2001:db8:abcd:12::1") == "2001:db8:abcd::/48"


# ── rate limiter memory ─────────────────────────────────────────────────────

def test_idle_keys_are_swept_so_spraying_random_keys_cannot_grow_memory_forever():
    limiter = RateLimiter(5, window_seconds=60.0)
    for i in range(3000):
        limiter.allow(f"key-{i}", now=0.0)
    assert len(limiter._hits) == 3000
    # a later burst of calls (beyond the sweep interval) after everything has aged out
    for i in range(RateLimiter._SWEEP_EVERY):
        limiter.allow("fresh", now=1000.0)
    assert len(limiter._hits) < 100


def test_asking_how_long_to_wait_does_not_create_keys():
    limiter = RateLimiter(1, 60.0)
    assert limiter.retry_after_seconds("never-seen") == 0.0
    assert "never-seen" not in limiter._hits


# ── global rate limit middleware ────────────────────────────────────────────

async def _call(app, path="/api/x", headers=(), peer=("203.0.113.9", 5000)):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "headers": list(headers), "client": peer}
    await app(scope, receive, send)
    return sent


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status(sent):
    return sent[0]["status"]


def _bearer(sub="alice", role="supervisor"):
    return (b"authorization", f"Bearer {auth.issue_token(SECRET, sub, role)}".encode())


def _mw(**kw):
    kw.setdefault("secret", SECRET)
    return GlobalRateLimitMiddleware(_ok_app, **kw)


def test_anonymous_traffic_is_limited_per_ip():
    mw = _mw(anonymous_per_minute=3)
    statuses = [_status(asyncio.run(_call(mw))) for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]


def test_the_429_has_a_retry_after_and_a_json_error():
    mw = _mw(anonymous_per_minute=1)
    asyncio.run(_call(mw))
    sent = asyncio.run(_call(mw))
    headers = dict(sent[0]["headers"])
    assert sent[0]["status"] == 429 and int(headers[b"retry-after"]) >= 1
    assert "error" in json.loads(sent[1]["body"])


def test_one_ip_being_throttled_does_not_affect_another():
    mw = _mw(anonymous_per_minute=1)
    asyncio.run(_call(mw, peer=("198.51.100.1", 1)))
    assert _status(asyncio.run(_call(mw, peer=("198.51.100.1", 1)))) == 429
    assert _status(asyncio.run(_call(mw, peer=("198.51.100.2", 1)))) == 200


def test_signed_in_staff_sharing_one_venue_ip_are_limited_per_login_not_per_ip():
    mw = _mw(anonymous_per_minute=1, identity_per_minute=1000)
    for i in range(30):   # 30 different phones behind one NAT, all comfortably above the anonymous limit
        sent = asyncio.run(_call(mw, headers=[_bearer(f"emp{i}", "employee")]))
        assert _status(sent) == 200


def test_a_single_runaway_login_is_throttled():
    mw = _mw(identity_per_minute=3)
    statuses = [_status(asyncio.run(_call(mw, headers=[_bearer()]))) for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]


def test_a_forged_token_is_treated_as_anonymous():
    mw = _mw(anonymous_per_minute=1, identity_per_minute=1000)
    forged = (b"authorization", b"Bearer " + auth.issue_token("other-secret-other-secret-other-1", "x", "admin").encode())
    asyncio.run(_call(mw, headers=[forged]))
    assert _status(asyncio.run(_call(mw, headers=[forged]))) == 429


def test_the_ip_total_ceiling_applies_even_to_signed_in_traffic():
    mw = _mw(ip_total_per_minute=2)
    statuses = [_status(asyncio.run(_call(mw, headers=[_bearer()]))) for _ in range(3)]
    assert statuses == [200, 200, 429]


def test_health_checks_and_websockets_are_never_throttled():
    mw = _mw(anonymous_per_minute=1, ip_total_per_minute=1)
    for _ in range(5):
        assert _status(asyncio.run(_call(mw, path="/healthz"))) == 200

    async def ws():
        sent = []
        async def receive(): return {"type": "websocket.connect"}
        async def send(m): sent.append(m)
        async def ws_app(scope, receive, send): await send({"type": "ok"})
        await GlobalRateLimitMiddleware(ws_app, secret=SECRET, anonymous_per_minute=0, ip_total_per_minute=0)(
            {"type": "websocket", "path": "/ws/app", "headers": [], "client": ("1.1.1.1", 1)}, receive, send)
        return sent
    assert asyncio.run(ws()) == [{"type": "ok"}]


def test_the_limited_callback_is_told_who_and_why():
    seen = []
    mw = _mw(anonymous_per_minute=1, on_limited=lambda why, ip, ident: seen.append((why, ip)))
    asyncio.run(_call(mw))
    asyncio.run(_call(mw))
    assert seen == [("anonymous", "203.0.113.9")]


def test_the_trusted_hop_setting_decides_which_address_is_limited():
    mw = _mw(anonymous_per_minute=1, trusted_hops=1)
    h = lambda fake: [(b"x-forwarded-for", f"{fake}, 203.0.113.9".encode())]
    asyncio.run(_call(mw, headers=h("1.1.1.1")))
    # same real client, different spoofed prefix: still the same bucket
    assert _status(asyncio.run(_call(mw, headers=h("2.2.2.2")))) == 429


# ── security events ─────────────────────────────────────────────────────────

class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def _monitor(thresholds=None):
    lines, clock = [], _Clock()
    def log(msg, level="info", **fields): lines.append((level, msg, fields))
    return SecurityMonitor(log, thresholds=thresholds or {"login_failed": (3, 60)}, clock=clock), lines, clock


def test_every_event_is_logged_with_the_right_level():
    m, lines, _ = _monitor()
    m.record("login_succeeded", username="alice")
    m.record("login_failed", keys=["ip:203.0.113.9"], username="alice")
    assert [l[0] for l in lines] == ["info", "warning"]
    assert lines[1][2]["security_event"] == "login_failed"


def test_repeated_failures_raise_exactly_one_error_level_alert_per_window():
    m, lines, clock = _monitor()
    for _ in range(10):
        m.record("login_failed", keys=["ip:203.0.113.9"])
        clock.t += 1
    alerts = [l for l in lines if l[0] == "error"]
    assert len(alerts) == 1
    assert "203.0.113.0/24" in alerts[0][1]       # truncated: this message goes to a third party
    assert "203.0.113.9" not in alerts[0][1]


def test_the_alert_rearms_after_the_window():
    m, lines, clock = _monitor()
    for _ in range(4):
        m.record("login_failed", keys=["ip:1.2.3.4"])
    clock.t += 120
    for _ in range(4):
        m.record("login_failed", keys=["ip:1.2.3.4"])
    assert len([l for l in lines if l[0] == "error"]) == 2


def test_failures_spread_below_the_threshold_do_not_alert():
    m, lines, clock = _monitor()
    for _ in range(10):
        m.record("login_failed", keys=["ip:1.2.3.4"])
        clock.t += 31        # never more than 2 within 60s
    assert not [l for l in lines if l[0] == "error"]


def test_different_keys_are_counted_separately():
    m, lines, _ = _monitor()
    for i in range(10):
        m.record("login_failed", keys=[f"ip:10.0.0.{i}"])
    assert not [l for l in lines if l[0] == "error"]


def test_phone_numbers_never_appear_in_an_alert():
    m, lines, _ = _monitor()
    for _ in range(4):
        m.record("login_failed", keys=["phone:+15551234567"])
    alert = [l for l in lines if l[0] == "error"][0]
    assert "5551234567" not in alert[1]


def test_a_logging_failure_never_breaks_the_caller():
    def broken(*a, **k): raise RuntimeError("log sink down")
    SecurityMonitor(broken).record("login_failed", keys=["ip:1"])   # must not raise
