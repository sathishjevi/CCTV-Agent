"""
Notification delivery layer — build brief Phase 4 task 3: "Wire a real
notification channel: Firebase Cloud Messaging for an employee app, or
Twilio SMS as a no-app fallback for the pilot. Keep this behind a config
flag so shadow mode can still be re-enabled instantly if needed."

Global Constraint 1 still applies at this boundary: this module never
decides *whether* to notify — it only ever sends what RulesEngine /
EffortEngine already decided to send (a Tier 1 nudge, or a Tier 2
directive the supervisor has explicitly approved via approve()). It has
no escalation or decision logic of its own. It is only ever reached via
`on_notify`, which engine.py/effort_engine.py call exclusively when
`shadow_mode` is False — so flipping `FLOORWATCH_SHADOW_MODE=true` back
on instantly stops every call into this module, independent of whatever
`FLOORWATCH_NOTIFY_CHANNEL` is configured.

Caveat, stated plainly rather than silently glossed over: no credentials
for a real Twilio account or Firebase project are available in this dev
sandbox. The Twilio/FCM SDK integration code below is real, production
code — but it has only been exercised in tests against mocked SDK
clients (tests/test_notifications.py), not verified against the real
Twilio/Firebase APIs. See PHASE_4_NOTES.md.

Added later: MSG91Sender and Fast2SmsSender — alternate SMS gateways,
selected via config.SMS_PROVIDER (see that config value's own comment),
requested specifically because Twilio is more expensive for India-market
SMS than these two. Same never-tested-against-the-real-API caveat as
Twilio/FCM above — this is real, production HTTP-calling code, exercised
here only against mocked HTTP responses.

One real architectural constraint, not a coding choice: India's TRAI DLT
regulation requires SMS content sent to Indian numbers to come from a
pre-approved template, on ANY gateway (this would apply to Twilio-for-
India too, not just these two). MSG91/Fast2SMS are India-specific
gateways and are ALWAYS template-only, regardless of config.SMS_COUNTRY —
both send this codebase's already-built free-text message as the single
value of one generic, pre-approved template's one variable, rather than
mapping each message kind (assignment/nudge/flag-confirmed/...) to its
own template. The customer registers ONE simple catch-all template (e.g.
"Floorwatch: {#var#}"), and every message this service ever sends flows
through it unchanged — trading a small amount of template rigidity for
not having to hardcode, or keep in sync with a customer's DLT approval,
a distinct template per message kind this codebase might ever produce.

Twilio, unlike those two, is general-purpose and can send either shape —
so for Twilio specifically, whether that same fixed-pattern treatment
applies is driven by config.SMS_COUNTRY (empty/unset = send freeform
text, unchanged from this module's original behavior; "IN" = wrap
through config.TWILIO_DLT_TEMPLATE first, same "one predictable pattern"
idea as MSG91/Fast2SMS above, since a Twilio number sending to Indian
recipients is subject to the identical TRAI DLT carrier-side matching).

Neither of these two gateways' INBOUND reply support (the employee-reply
half of the task workflow: START/DONE/MORE/REVIEW) is wired up here.
MSG91's two-way SMS is a separate product (a purchased Virtual Number/
Long Code + "inbox balance"), and Fast2SMS's inbound-SMS webhook support
is not clearly documented as covering plain SMS specifically. Only
Twilio's inbound webhook (main.py's /api/webhooks/twilio-sms) is wired —
switching SMS_PROVIDER away from "twilio" affects OUTBOUND sends only;
employees would still need to be able to text the Twilio number back (or
this needs a follow-up integration once one of these providers' real
inbound behavior is confirmed against their live docs/account).
"""

import json
from pathlib import Path
from typing import Optional

from floorwatch_logging import get_logger

log = get_logger("rules-engine.notifications")


def _mask_phone(number: Optional[str]) -> Optional[str]:
    """SECURITY_REVIEW.md M2 — phone numbers were logged in full on every
    send attempt. If logs are ever aggregated to a shared store without
    redaction, that's employee/supervisor PII sitting in plaintext,
    potentially with broader access than the contact directory itself."""
    if not number:
        return number
    return f"***{number[-4:]}" if len(number) > 4 else "***"


def _mask_token(token: Optional[str]) -> Optional[str]:
    """Same rationale as _mask_phone, for FCM device tokens."""
    if not token:
        return token
    return f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"


def _mask_context(to_context: dict) -> dict:
    return {
        "phone": _mask_phone(to_context.get("phone")) if "phone" in to_context else None,
        "fcm_token": _mask_token(to_context.get("fcm_token")) if "fcm_token" in to_context else None,
    }


class ContactBook:
    """Pilot contact directory — zone_id -> employee phone/FCM token, plus
    a single supervisor phone. Manually maintained JSON file, matching
    roster.json's pilot-scale pattern (see contacts.json at the service
    root). Reloaded on every lookup so it can be edited without a restart."""

    def __init__(self, contacts_path: Path):
        self.contacts_path = contacts_path

    def _load(self) -> dict:
        if not self.contacts_path.exists():
            return {}
        try:
            return json.loads(self.contacts_path.read_text())
        except json.JSONDecodeError:
            return {}

    def phone_for_zone(self, zone_id: str) -> Optional[str]:
        return self._load().get("zones", {}).get(zone_id, {}).get("employee_phone")

    def fcm_token_for_zone(self, zone_id: str) -> Optional[str]:
        return self._load().get("zones", {}).get(zone_id, {}).get("employee_fcm_token")

    def supervisor_phone(self) -> Optional[str]:
        return self._load().get("supervisor_phone")


class NotificationResult:
    def __init__(self, sent: bool, channel: str, detail: str = ""):
        self.sent = sent
        self.channel = channel
        self.detail = detail

    def to_dict(self) -> dict:
        return {"sent": self.sent, "channel": self.channel, "detail": self.detail}


class NoOpSender:
    """The channel used whenever NOTIFY_CHANNEL is unset/unrecognized, or
    a real sender failed to initialize. Never contacts anything — only
    logs and records the attempt, so a bad config degrades to shadow-mode
    behavior rather than crashing the delivery path."""

    def send(self, to_context: dict, message: str) -> NotificationResult:
        log(f'NO-OP channel: would send "{message}" to {_mask_context(to_context)}')
        return NotificationResult(sent=False, channel="noop", detail="no channel configured")


class TwilioSmsSender:
    def __init__(self, account_sid: str, auth_token: str, from_number: str, dlt_template: Optional[str] = None):
        from twilio.rest import Client  # imported lazily so `twilio` is only required if this channel is used
        self._client = Client(account_sid, auth_token)
        self.from_number = from_number
        # Set only when config.SMS_COUNTRY requires it (India today — see
        # that config value's comment). None means send this codebase's
        # message text as-is, exactly like before this feature existed —
        # every deployment that hasn't set FLOORWATCH_SMS_COUNTRY keeps
        # its current behavior unchanged.
        self.dlt_template = dlt_template

    def send(self, to_context: dict, message: str) -> NotificationResult:
        to_number = to_context.get("phone")
        if not to_number:
            log(f"No phone number on file for {_mask_context(to_context)} — skipping SMS send")
            return NotificationResult(sent=False, channel="twilio_sms", detail="no phone on file")
        # DLT compliance is enforced by the destination carrier matching
        # the ACTUAL transmitted text against a pre-registered pattern —
        # Twilio has no template_id API param the way MSG91/Fast2SMS do,
        # so the only lever this code has is sending a fixed, predictable
        # shape instead of the fully free-form sentence other callers see.
        body = self.dlt_template.format(message=message) if self.dlt_template else message
        try:
            msg = self._client.messages.create(to=to_number, from_=self.from_number, body=body)
            log(f"Twilio SMS sent to {_mask_phone(to_number)}, sid={msg.sid}")
            return NotificationResult(sent=True, channel="twilio_sms", detail=msg.sid)
        except Exception as e:
            log(f"Twilio send failed: {e}")
            return NotificationResult(sent=False, channel="twilio_sms", detail=str(e))


class Msg91SmsSender:
    """MSG91 Flow API. See this module's docstring for the single-generic-
    template design and the (not wired up) inbound-reply caveat."""

    def __init__(self, auth_key: str, template_id: str, variable_name: str):
        if not auth_key or not template_id:
            raise ValueError("MSG91 requires FLOORWATCH_MSG91_AUTH_KEY and FLOORWATCH_MSG91_TEMPLATE_ID")
        import httpx  # already a direct dependency of this service (requirements.txt) — imported
                       # lazily to match this module's other senders, and so a deployment that
                       # never uses this channel doesn't need to reason about the import at load time
        self._httpx = httpx
        self.auth_key = auth_key
        self.template_id = template_id
        self.variable_name = variable_name

    def send(self, to_context: dict, message: str) -> NotificationResult:
        to_number = to_context.get("phone")
        if not to_number:
            log(f"No phone number on file for {_mask_context(to_context)} — skipping SMS send")
            return NotificationResult(sent=False, channel="msg91_sms", detail="no phone on file")
        mobile = to_number.lstrip("+")
        try:
            resp = self._httpx.post(
                "https://control.msg91.com/api/v5/flow",
                headers={"authkey": self.auth_key, "Content-Type": "application/json"},
                json={
                    "template_id": self.template_id,
                    "recipients": [{"mobiles": mobile, self.variable_name: message}],
                },
                timeout=10,
            )
            body = resp.json() if resp.content else {}
            if resp.status_code == 200 and body.get("type") != "error":
                log(f"MSG91 SMS sent to {_mask_phone(to_number)}, response={body.get('message', body)}")
                return NotificationResult(sent=True, channel="msg91_sms", detail=str(body.get("message", "")))
            log(f"MSG91 send failed (HTTP {resp.status_code}): {body}")
            return NotificationResult(sent=False, channel="msg91_sms", detail=str(body))
        except Exception as e:
            log(f"MSG91 send failed: {e}")
            return NotificationResult(sent=False, channel="msg91_sms", detail=str(e))


class Fast2SmsSender:
    """Fast2SMS DLT route (bulkV2). See this module's docstring for the
    single-generic-template design and the (not wired up) inbound-reply
    caveat. Confusingly, Fast2SMS's `message` field for this route is the
    approved template's id, not message text — `message_id` here to avoid
    that confusion at the call site."""

    def __init__(self, api_key: str, sender_id: str, message_id: str):
        if not api_key or not sender_id or not message_id:
            raise ValueError("Fast2SMS requires FLOORWATCH_FAST2SMS_API_KEY, "
                              "FLOORWATCH_FAST2SMS_SENDER_ID, and FLOORWATCH_FAST2SMS_MESSAGE_ID")
        import httpx  # lazy import, same rationale as Msg91SmsSender above
        self._httpx = httpx
        self.api_key = api_key
        self.sender_id = sender_id
        self.message_id = message_id

    def send(self, to_context: dict, message: str) -> NotificationResult:
        to_number = to_context.get("phone")
        if not to_number:
            log(f"No phone number on file for {_mask_context(to_context)} — skipping SMS send")
            return NotificationResult(sent=False, channel="fast2sms_sms", detail="no phone on file")
        digits = to_number.lstrip("+")
        # Fast2SMS's DLT route expects a bare 10-digit Indian mobile number,
        # no country code — this provider is India-specific by design, so
        # stripping a leading "91" (from a +91-prefixed stored number) is a
        # safe, targeted normalization rather than a general phone parser.
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        try:
            resp = self._httpx.post(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                json={
                    "route": "dlt",
                    "sender_id": self.sender_id,
                    "message": self.message_id,
                    "variables_values": message,
                    "numbers": digits,
                },
                timeout=10,
            )
            body = resp.json() if resp.content else {}
            if resp.status_code == 200 and body.get("return") is True:
                log(f"Fast2SMS sent to {_mask_phone(to_number)}, request_id={body.get('request_id')}")
                return NotificationResult(sent=True, channel="fast2sms_sms", detail=str(body.get("request_id", "")))
            log(f"Fast2SMS send failed (HTTP {resp.status_code}): {body}")
            return NotificationResult(sent=False, channel="fast2sms_sms", detail=str(body))
        except Exception as e:
            log(f"Fast2SMS send failed: {e}")
            return NotificationResult(sent=False, channel="fast2sms_sms", detail=str(e))


class FcmSender:
    def __init__(self, credentials_path: str):
        import firebase_admin
        from firebase_admin import credentials
        if not firebase_admin._apps:
            cred = credentials.Certificate(credentials_path)
            firebase_admin.initialize_app(cred)

    def send(self, to_context: dict, message: str) -> NotificationResult:
        from firebase_admin import messaging
        token = to_context.get("fcm_token")
        if not token:
            log(f"No FCM token on file for {_mask_context(to_context)} — skipping push send")
            return NotificationResult(sent=False, channel="fcm", detail="no fcm token on file")
        try:
            msg = messaging.Message(
                notification=messaging.Notification(title="Floorwatch", body=message),
                token=token,
            )
            message_id = messaging.send(msg)
            log(f"FCM push sent, message_id={message_id}")
            return NotificationResult(sent=True, channel="fcm", detail=message_id)
        except Exception as e:
            log(f"FCM send failed: {e}")
            return NotificationResult(sent=False, channel="fcm", detail=str(e))


def build_sender(channel: str, config):
    """channel: "none" | "twilio" | "sms" | "fcm". Never raises — falls
    back to NoOpSender on any initialization failure (missing/invalid
    credentials, missing SDK) so a bad deployment config can't crash the
    rules engine's delivery path; it just silently behaves like shadow
    mode for real sends.

    "twilio" and "sms" are both treated as "the SMS channel" and
    dispatched to whichever concrete gateway config.SMS_PROVIDER names —
    see that config value's own comment for why this indirection exists
    (switching gateways without touching NOTIFY_CHANNEL, employee_directory's
    per-employee "sms" value, or TASK_CHANNEL_SENDERS' keys). A deployment
    that has never set FLOORWATCH_SMS_PROVIDER keeps its exact prior
    behavior — the default is "twilio"."""
    if channel in ("twilio", "sms"):
        # getattr, not a direct attribute access: a config object that
        # predates SMS_PROVIDER (e.g. a test's minimal FakeConfig) must
        # still get Twilio, its pre-existing behavior — not an
        # AttributeError, which would break this function's own
        # never-raises contract.
        provider = getattr(config, "SMS_PROVIDER", "twilio")
        try:
            if provider == "msg91":
                return Msg91SmsSender(config.MSG91_AUTH_KEY, config.MSG91_TEMPLATE_ID, config.MSG91_VARIABLE_NAME)
            if provider == "fast2sms":
                return Fast2SmsSender(config.FAST2SMS_API_KEY, config.FAST2SMS_SENDER_ID, config.FAST2SMS_MESSAGE_ID)
            # Twilio is the one gateway here that CAN send either freeform
            # or DLT-templated text — see config.SMS_COUNTRY's comment.
            # getattr guards both attributes for the same reason as
            # SMS_PROVIDER above: a config object that predates this
            # feature must keep sending freeform, not raise.
            country = getattr(config, "SMS_COUNTRY", "")
            dlt_required_countries = getattr(config, "DLT_REQUIRED_COUNTRIES", set())
            dlt_template = getattr(config, "TWILIO_DLT_TEMPLATE", None) if country in dlt_required_countries else None
            return TwilioSmsSender(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN, config.TWILIO_FROM_NUMBER,
                                    dlt_template=dlt_template)
        except Exception as e:
            log(f"could not initialize {provider} SMS sender ({e}) — falling back to NoOpSender", level="warning")
            return NoOpSender()
    if channel == "fcm":
        try:
            return FcmSender(config.FCM_CREDENTIALS_PATH)
        except Exception as e:
            log(f"could not initialize FCM sender ({e}) — falling back to NoOpSender", level="warning")
            return NoOpSender()
    return NoOpSender()


class NotificationDispatcher:
    """Bridges engine.py/effort_engine.py's on_notify(channel, event) hook
    to a concrete sender. `channel` here is the semantic kind of message
    ("employee_nudge", "supervisor_directive_send", "employee_low_effort_nudge"),
    not the transport — the transport is whichever `sender` was built."""

    def __init__(self, sender, contacts: ContactBook):
        self.sender = sender
        self.contacts = contacts

    def __call__(self, kind: str, event: dict) -> NotificationResult:
        message = event.get("message") or f"Floorwatch: {event.get('event_type')}"
        zone_id = event.get("zone_id")

        if kind in ("employee_nudge", "employee_low_effort_nudge"):
            to_context = {
                "phone": self.contacts.phone_for_zone(zone_id) if zone_id else None,
                "fcm_token": self.contacts.fcm_token_for_zone(zone_id) if zone_id else None,
            }
        elif kind == "supervisor_directive_send":
            to_context = {"phone": self.contacts.supervisor_phone()}
        else:
            to_context = {}

        result = self.sender.send(to_context, message)
        event["notification_result"] = result.to_dict()
        return result
