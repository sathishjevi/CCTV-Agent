"""Error monitoring (PostHog Error Tracking) — off unless an API key is set.

Without this, a crash or a failing notification is only visible to someone
reading Railway's raw log stream. With FLOORWATCH_POSTHOG_API_KEY set on
Railway:
  - exceptions that escape a request handler (would-be HTTP 500s) are
    reported, via the ASGI middleware installed by main.py;
  - uncaught exceptions elsewhere (threads, the process itself) are
    reported by the SDK's own exception autocapture;
  - anything the service logs at level "error"/"critical" is reported too,
    via floorwatch_logging's error-reporter hook — that's how a failed push
    or a crashed background loop raises an alert without each call site
    knowing about PostHog.

What is NOT sent: request bodies, headers, cookies, query strings, function
local variables, or user identities. Events carry one fixed service-level
distinct_id, never an employee number or a username, and anything that
looks like a bearer token or JWT is scrubbed from text before it leaves.
GeoIP enrichment is off.

The SDK is imported lazily and every failure here is swallowed — monitoring
must never be the thing that takes the service down."""

import re
from typing import Optional

from floorwatch_logging import get_logger, set_error_reporter

log = get_logger("rules-engine.error_monitoring")

# One fixed identity for the whole service — deliberately NOT a person.
DISTINCT_ID = "floorwatch-rules-engine"

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

_client = None


def scrub(text):
    if not isinstance(text, str):
        return text
    return _JWT.sub("[token]", _BEARER.sub("Bearer [token]", text))


def _scrub_value(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _before_send(event):
    """PostHog before_send hook: scrub every string in the event; never
    let a scrubbing bug drop or crash an event."""
    try:
        return _scrub_value(event)
    except Exception:
        return event


def init_error_monitoring(api_key: str, host: str = "", environment: str = "production",
                          release: str = "") -> bool:
    """Returns True if monitoring is now active, False if it's off (no key)
    or couldn't start. Never raises."""
    global _client
    if not api_key:
        return False
    try:
        from posthog import Posthog
    except ImportError:
        log("A PostHog API key is set but the posthog package isn't installed — "
            "error monitoring is OFF.", level="warning")
        return False
    try:
        kwargs = dict(
            enable_exception_autocapture=True,
            capture_exception_code_variables=False,  # locals can hold secrets
            disable_geoip=True,
            before_send=_before_send,
            super_properties={
                "service": "rules-engine", "environment": environment,
                **({"release": release} if release else {}),
            },
        )
        if host:
            kwargs["host"] = host
        _client = Posthog(api_key, **kwargs)

        def _report(service: str, message: str, fields: dict):
            _client.capture_exception(
                RuntimeError(scrub(message)), distinct_id=DISTINCT_ID,
                properties={"logger": service, "logged_error": True})

        set_error_reporter(_report)
        log(f"Error monitoring enabled (PostHog, environment={environment}).")
        return True
    except Exception as e:
        _client = None
        log(f"Could not start error monitoring ({type(e).__name__}) — continuing without it.", level="warning")
        return False


def capture_request_exception(exc: BaseException, method: str = "", route: str = ""):
    """Reports an exception that escaped a request handler. Only the HTTP
    method and the ROUTE TEMPLATE (e.g. /api/tasks/{task_id}, not the real
    id) are attached — no bodies, headers, query strings or identities."""
    if _client is None:
        return
    try:
        _client.capture_exception(
            exc, distinct_id=DISTINCT_ID,
            properties={"http_method": method, "route": route or "unknown", "unhandled_in_request": True})
    except Exception:
        pass


def shutdown():
    """Flushes queued events — call on service shutdown so the last errors
    before a restart aren't lost."""
    global _client
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception:
        pass
    _client = None


class ErrorReportingMiddleware:
    """Pure-ASGI middleware: reports any exception that escapes the app,
    then re-raises it so normal 500 handling is unchanged. Exceptions the
    app deliberately turns into responses (HTTPException, validation
    errors) are handled deeper in the stack and never reach here."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            route = getattr(scope.get("route"), "path", "") or ""
            capture_request_exception(exc, scope.get("method", scope.get("type", "")), route)
            raise
