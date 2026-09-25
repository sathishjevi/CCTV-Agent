"""Error monitoring (Sentry) — off unless a DSN is configured.

Without this, a crash or a failing notification is only visible to someone
reading Railway's raw log stream. With FLOORWATCH_SENTRY_DSN (or SENTRY_DSN)
set on Railway:
  - every unhandled exception in a request is reported (Sentry's FastAPI
    integration does this by itself), and
  - anything the service logs at level "error"/"critical" is reported too,
    via floorwatch_logging's error-reporter hook — that's how a failed push
    or a crashed background loop raises an alert without each call site
    knowing about Sentry.

Personal data stays out: send_default_pii is off (no request bodies,
cookies or auth headers), performance tracing is off by default, and
anything that looks like a bearer token or JWT is scrubbed before it leaves.

The SDK is imported lazily and every failure here is swallowed — monitoring
must never be the thing that takes the service down."""

import re

from floorwatch_logging import get_logger, set_error_reporter

log = get_logger("rules-engine.error_monitoring")

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def scrub(text):
    if not isinstance(text, str):
        return text
    return _JWT.sub("[token]", _BEARER.sub("Bearer [token]", text))


def _before_send(event, hint):
    try:
        if isinstance(event.get("message"), str):
            event["message"] = scrub(event["message"])
        logentry = event.get("logentry")
        if isinstance(logentry, dict) and isinstance(logentry.get("message"), str):
            logentry["message"] = scrub(logentry["message"])
        request = event.get("request")
        if isinstance(request, dict):
            request.pop("cookies", None)
            request.pop("data", None)
            headers = request.get("headers")
            if isinstance(headers, dict):
                for key in list(headers):
                    if key.lower() in ("authorization", "cookie", "x-api-key"):
                        headers[key] = "[redacted]"
    except Exception:
        pass
    return event


def init_error_monitoring(dsn: str, environment: str = "production", release: str = "",
                          traces_sample_rate: float = 0.0) -> bool:
    """Returns True if monitoring is now active, False if it's off (no DSN)
    or couldn't start. Never raises."""
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        log("A Sentry DSN is set but sentry-sdk isn't installed — error monitoring is OFF.", level="warning")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn, environment=environment, release=release or None,
            send_default_pii=False, traces_sample_rate=traces_sample_rate,
            before_send=_before_send,
        )

        def _report(service: str, message: str, fields: dict):
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("service", service)
                scope.set_level("error")
                sentry_sdk.capture_message(scrub(message))

        set_error_reporter(_report)
        log(f"Error monitoring enabled (environment={environment}).")
        return True
    except Exception as e:
        log(f"Could not start error monitoring ({type(e).__name__}) — continuing without it.", level="warning")
        return False
