"""Inbound-SMS handling for the task workflow — an employee replies
START/DONE/MORE/REVIEW (optionally with a task short code) to the
assignment message they received, and this maps that back onto a real
task transition. Split out from main.py for testability: signature
validation and keyword parsing are pure/mockable, kept separate from the
FastAPI route wiring itself.

Security note: Twilio's webhook has no bearer-token auth (Twilio doesn't
have one to send) — X-Twilio-Signature is the ONLY thing standing
between this endpoint and anyone on the internet POSTing fake "employee
replies" that mark tasks complete. validate_signature() below is not
optional scaffolding; main.py's route must reject anything that fails it,
every time, with no bypass.
"""

import re
from typing import Optional

from floorwatch_logging import get_logger

_log = get_logger("rules-engine.sms_webhook")

# Keyword -> normalized action. Matched case-insensitively, optionally
# followed by a task short code (see effort_engine.py's short_code()/
# resolve_task_reference()). A few common synonyms included since this
# is SMS from a phone keyboard, not a form with a dropdown.
_KEYWORD_ALIASES = {
    "START": "START", "STARTED": "START", "BEGIN": "START",
    "DONE": "DONE", "COMPLETE": "DONE", "COMPLETED": "DONE", "FINISHED": "DONE",
    "MORE": "MORE", "EXTEND": "MORE", "EXTENSION": "MORE",
    "REVIEW": "REVIEW", "HELP": "REVIEW",
}
VALID_ACTIONS = {"START", "DONE", "MORE", "REVIEW"}


def parse_sms_command(body: str):
    """Returns (action, code) — action is one of VALID_ACTIONS or None if
    unrecognized; code is the task short code if one was included, else
    None. "start" / "START ABC123" / "  Done  " all parse correctly —
    real people don't type consistently on a phone keyboard."""
    text = (body or "").strip()
    if not text:
        return None, None
    parts = text.split(None, 1)
    keyword = _KEYWORD_ALIASES.get(parts[0].strip().upper())
    if keyword is None:
        return None, None
    code = None
    if len(parts) > 1:
        candidate = re.sub(r"[^A-Za-z0-9]", "", parts[1])
        if candidate:
            code = candidate.upper()
    return keyword, code


def validate_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """Wraps twilio.request_validator.RequestValidator — imported lazily
    so `twilio` stays an optional dependency for deployments that never
    configure this channel, matching notifications.py's own lazy-import
    pattern for the same package. Returns False (never raises) on any
    validation error, including a missing/empty auth_token — an empty
    token must NEVER be treated as "skip validation"."""
    if not auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
        return RequestValidator(auth_token).validate(url, params, signature)
    except Exception as e:
        _log(f"signature validation error: {e}", level="warning")
        return False


def reply_twiml(message: str) -> str:
    """Twilio expects a TwiML XML response for auto-replies — escaping is
    needed here too (message text ultimately comes from task names /
    employee input, same discipline as the dashboard's escapeHtml())."""
    escaped = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
