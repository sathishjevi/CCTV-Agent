"""Request-level guards shared by both Floorwatch services: who is the caller
(client IP behind a proxy) and a global request-rate limit.

WHY THIS EXISTS
  * Client IP. The old helper took the LEFTMOST X-Forwarded-For entry. A
    reverse proxy APPENDS the address it saw to whatever the client already
    put in that header, so the leftmost entry is attacker-controlled: sending
    a fresh random "X-Forwarded-For" with every login attempt made the
    per-IP login limiter useless. The entry to trust is the one the nearest
    trusted proxy added — the RIGHTMOST for one proxy hop. Deployments with
    more than one proxy in front (e.g. a CDN in front of Railway) set
    FLOORWATCH_TRUSTED_PROXY_HOPS accordingly.
  * Global rate limit. Only login, OTP, admin and chat endpoints had limits;
    every other route could be hammered. This adds a coarse ceiling over
    everything, deliberately generous: the point is to stop floods and
    runaway clients, not to shape normal traffic. Staff phones share one
    public IP at a venue (NAT), so authenticated traffic is limited PER
    LOGIN, not per IP; only anonymous traffic is limited per IP tightly.

Counters are in-process (same honest limitation as floorwatch_rate_limit:
with N replicas the effective ceiling is N times higher)."""

import json
from typing import Callable, Mapping, Optional

from floorwatch_auth import verify_token
from floorwatch_rate_limit import RateLimiter


def client_ip_from_headers(headers: Mapping[str, str], peer: Optional[str], trusted_hops: int = 1) -> str:
    """`headers` must support case-insensitive .get (Starlette's Headers does).
    `trusted_hops` is how many proxies sit in front of the app and append to
    X-Forwarded-For; 0 means "no proxy, never trust the header"."""
    if trusted_hops <= 0:
        return peer or "unknown"
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            # The last `trusted_hops` entries were appended by OUR proxies;
            # the entry just before them is the address our outermost trusted
            # proxy saw — the real client. Anything further left is claimed by
            # the client and ignored.
            index = max(0, len(parts) - trusted_hops)
            return parts[index]
    return peer or "unknown"


def truncate_ip(ip: str) -> str:
    """For anything leaving our infrastructure (alerts to a monitoring
    vendor): IPv4 -> /24, IPv6 -> /48. Local security logs keep the full
    address; third parties get only what's needed to recognise a network."""
    if ":" in ip:
        groups = ip.split(":")
        return ":".join(groups[:3]) + "::/48"
    octets = ip.split(".")
    if len(octets) == 4:
        return ".".join(octets[:3]) + ".0/24"
    return ip


def _client_ip_from_scope(scope: dict, trusted_hops: int) -> str:
    headers = {}
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            headers["x-forwarded-for"] = v.decode("latin-1")
            break
    client = scope.get("client")
    return client_ip_from_headers(headers, client[0] if client else None, trusted_hops)


class GlobalRateLimitMiddleware:
    """Pure-ASGI middleware (no BaseHTTPMiddleware overhead, and it never
    touches WebSockets or request bodies).

      * ip_total_per_minute  — ceiling on ANY traffic from one IP.
      * anonymous_per_minute — requests without a valid token, per IP. Real
        anonymous traffic is login, the dashboard page and webhooks.
      * identity_per_minute  — per signed-in login (role + subject).
    """

    def __init__(self, app, *, secret: str, anonymous_per_minute: int = 120,
                 identity_per_minute: int = 1200, ip_total_per_minute: int = 6000,
                 trusted_hops: int = 1, exempt_paths: tuple = ("/healthz",),
                 on_limited: Optional[Callable[[str, str, str], None]] = None):
        self.app = app
        self.secret = secret
        self.trusted_hops = trusted_hops
        self.exempt_paths = exempt_paths
        self.on_limited = on_limited
        self._anonymous = RateLimiter(anonymous_per_minute, 60.0)
        self._identity = RateLimiter(identity_per_minute, 60.0)
        self._ip_total = RateLimiter(ip_total_per_minute, 60.0)

    def _identity_key(self, scope: dict) -> Optional[str]:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                value = v.decode("latin-1")
                if value.startswith("Bearer "):
                    payload = verify_token(self.secret, value[7:])
                    if payload and "sub" in payload:
                        return f"{payload.get('role', '?')}:{payload['sub']}"
                return None
        return None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        ip = _client_ip_from_scope(scope, self.trusted_hops)
        identity = self._identity_key(scope)

        limited_by = None
        limiter, key = None, None
        if not self._ip_total.allow(ip):
            limited_by, limiter, key = "ip_total", self._ip_total, ip
        elif identity is not None:
            if not self._identity.allow(identity):
                limited_by, limiter, key = "identity", self._identity, identity
        elif not self._anonymous.allow(ip):
            limited_by, limiter, key = "anonymous", self._anonymous, ip

        if limited_by is None:
            await self.app(scope, receive, send)
            return

        if self.on_limited:
            try:
                self.on_limited(limited_by, ip, identity or "")
            except Exception:
                pass
        retry_after = int(limiter.retry_after_seconds(key)) + 1
        body = json.dumps({"error": "Too many requests — slow down and try again shortly."}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(retry_after).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})


def validation_error_body(errors: list) -> dict:
    """Body for a 422: where each problem is and why — never the submitted
    value. FastAPI's default echoes every bad field's INPUT back (for a
    rejected password field, that is the password). `error` is a plain
    string so the apps can show it; they read `error`, and used to choke on
    the default list-shaped `detail`."""
    problems = [{"field": ".".join(str(part) for part in e["loc"] if part != "body"),
                 "problem": e["msg"], "type": e["type"]} for e in errors]
    first = problems[0] if problems else {"field": "", "problem": "invalid request"}
    where = f"'{first['field']}': " if first["field"] else ""
    return {"error": f"Invalid request — {where}{first['problem']}", "detail": problems}


def install_error_handlers(app, monitor, client_ip_fn: Callable) -> None:
    """Registers, on a FastAPI app:
      * a validation-error handler that doesn't echo input (above);
      * an HTTP-error handler that returns exactly the usual responses but
        also records authentication/authorization FAILURES and unknown-route
        probes as security events, so repeated ones can alert. The route
        TEMPLATE is logged, never the real path (it may hold employee numbers
        or task ids)."""
    from fastapi.exception_handlers import http_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request, exc):
        kind = {401: "auth_invalid_token", 403: "authz_denied", 404: "not_found"}.get(exc.status_code)
        if kind:
            route = request.scope.get("route")
            ip = client_ip_fn(request)
            monitor.record(kind, keys=[f"ip:{ip}"], ip=ip, method=request.method,
                           route=getattr(route, "path", None) or "(no matching route)")
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc):
        return JSONResponse(status_code=422, content=validation_error_body(exc.errors()))
