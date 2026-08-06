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
"""

import json
import sys
from pathlib import Path
from typing import Optional


def log(msg: str):
    print(f"[notifications] {msg}", file=sys.stderr, flush=True)


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
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        from twilio.rest import Client  # imported lazily so `twilio` is only required if this channel is used
        self._client = Client(account_sid, auth_token)
        self.from_number = from_number

    def send(self, to_context: dict, message: str) -> NotificationResult:
        to_number = to_context.get("phone")
        if not to_number:
            log(f"No phone number on file for {_mask_context(to_context)} — skipping SMS send")
            return NotificationResult(sent=False, channel="twilio_sms", detail="no phone on file")
        try:
            msg = self._client.messages.create(to=to_number, from_=self.from_number, body=message)
            log(f"Twilio SMS sent to {_mask_phone(to_number)}, sid={msg.sid}")
            return NotificationResult(sent=True, channel="twilio_sms", detail=msg.sid)
        except Exception as e:
            log(f"Twilio send failed: {e}")
            return NotificationResult(sent=False, channel="twilio_sms", detail=str(e))


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
    """channel: "none" | "twilio" | "fcm". Never raises — falls back to
    NoOpSender on any initialization failure (missing/invalid credentials,
    missing SDK) so a bad deployment config can't crash the rules engine's
    delivery path; it just silently behaves like shadow mode for real sends."""
    if channel == "twilio":
        try:
            return TwilioSmsSender(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN, config.TWILIO_FROM_NUMBER)
        except Exception as e:
            log(f"WARNING: could not initialize Twilio sender ({e}) — falling back to NoOpSender")
            return NoOpSender()
    if channel == "fcm":
        try:
            return FcmSender(config.FCM_CREDENTIALS_PATH)
        except Exception as e:
            log(f"WARNING: could not initialize FCM sender ({e}) — falling back to NoOpSender")
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
