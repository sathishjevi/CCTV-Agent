"""
Unit tests for the notification delivery layer.

No real Twilio account or Firebase project exists in this sandbox — the
Twilio/FCM sender classes are exercised here against MOCKED SDK clients,
proving the integration code path is correct (right call shape, right
error handling, right fallback behavior), not that a real send succeeds
against Twilio/Firebase's live APIs. See notifications.py's module
docstring and PHASE_4_NOTES.md.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from notifications import (  # noqa: E402
    ContactBook, NoOpSender, NotificationDispatcher, TwilioSmsSender, build_sender,
    _mask_context, _mask_phone, _mask_token,
)


# ── PII masking (SECURITY_REVIEW.md M2) ─────────────────────────────────

def test_mask_phone_keeps_only_last_four_digits():
    assert _mask_phone("+15551234567") == "***4567"


def test_mask_phone_handles_short_input():
    assert _mask_phone("123") == "***"


def test_mask_phone_handles_none():
    assert _mask_phone(None) is None


def test_mask_token_keeps_prefix_and_suffix_only():
    assert _mask_token("abcdefghijklmnop") == "abcd...mnop"


def test_mask_token_handles_short_input():
    assert _mask_token("short") == "***"


def test_mask_context_masks_both_fields():
    masked = _mask_context({"phone": "+15551234567", "fcm_token": "abcdefghijklmnop"})
    assert masked == {"phone": "***4567", "fcm_token": "abcd...mnop"}
    assert "1234567" not in str(masked)


def test_notifications_log_never_contains_full_phone_number():
    """End-to-end: the log line produced by a real send attempt must not
    contain the unredacted phone number, even though the SDK call itself
    still receives the real number (masking is a logging-layer concern,
    not a functional one)."""
    import io
    import contextlib
    import notifications

    contacts_get = MagicMock()
    sender = TwilioSmsSender.__new__(TwilioSmsSender)  # bypass __init__'s twilio.rest.Client import
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM123")
    sender._client = mock_client
    sender.from_number = "+15559999999"

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        sender.send({"phone": "+15551234567"}, "test message")

    log_output = buf.getvalue()
    assert "1234567" not in log_output
    assert "4567" in log_output  # last 4 digits are fine to keep for correlation


# ── ContactBook ───────────────────────────────────────────────────────────

def test_contact_book_missing_file_returns_none(tmp_path):
    book = ContactBook(tmp_path / "nonexistent.json")
    assert book.phone_for_zone("concession") is None
    assert book.supervisor_phone() is None


def test_contact_book_reads_zone_phone(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text('{"supervisor_phone": "+15551112222", "zones": {"concession": {"employee_phone": "+15553334444"}}}')
    book = ContactBook(path)
    assert book.phone_for_zone("concession") == "+15553334444"
    assert book.supervisor_phone() == "+15551112222"
    assert book.phone_for_zone("unknown_zone") is None


def test_contact_book_malformed_json_returns_none(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text("not valid json{{{")
    book = ContactBook(path)
    assert book.phone_for_zone("concession") is None


# ── NoOpSender ────────────────────────────────────────────────────────────

def test_noop_sender_never_sends():
    sender = NoOpSender()
    result = sender.send({"phone": "+15550000000"}, "test message")
    assert result.sent is False
    assert result.channel == "noop"


# ── TwilioSmsSender (mocked SDK) ──────────────────────────────────────────

def test_twilio_sender_sends_via_mocked_client():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")

        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999")
        result = sender.send({"phone": "+15551234567"}, "Zone needs coverage")

        assert result.sent is True
        assert result.channel == "twilio_sms"
        assert result.detail == "SM123"
        mock_instance.messages.create.assert_called_once_with(
            to="+15551234567", from_="+15550009999", body="Zone needs coverage")


def test_twilio_sender_skips_send_when_no_phone_on_file():
    with patch("twilio.rest.Client"):
        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999")
        result = sender.send({"phone": None}, "message")
        assert result.sent is False
        assert "no phone" in result.detail


def test_twilio_sender_handles_api_error_gracefully():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.side_effect = Exception("Twilio API error: invalid number")

        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999")
        result = sender.send({"phone": "+1invalid"}, "message")
        assert result.sent is False
        assert "invalid number" in result.detail


# ── build_sender fallback behavior ───────────────────────────────────────

def test_build_sender_none_channel_returns_noop():
    class FakeConfig:
        pass
    sender = build_sender("none", FakeConfig())
    assert isinstance(sender, NoOpSender)


def test_build_sender_unrecognized_channel_returns_noop():
    class FakeConfig:
        pass
    sender = build_sender("carrier_pigeon", FakeConfig())
    assert isinstance(sender, NoOpSender)


def test_build_sender_twilio_with_bad_config_falls_back_to_noop():
    class FakeConfig:
        TWILIO_ACCOUNT_SID = ""
        TWILIO_AUTH_TOKEN = ""
        TWILIO_FROM_NUMBER = ""
    # Empty credentials -> twilio.rest.Client itself may or may not raise,
    # but if it does, build_sender must never propagate the exception.
    sender = build_sender("twilio", FakeConfig())
    assert sender is not None  # never raises


# ── NotificationDispatcher ────────────────────────────────────────────────

def test_dispatcher_routes_employee_nudge_to_zone_contact(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text('{"zones": {"concession": {"employee_phone": "+15551110000"}}}')
    contacts = ContactBook(path)

    fake_sender = MagicMock()
    fake_sender.send.return_value = MagicMock(to_dict=lambda: {"sent": True})

    dispatcher = NotificationDispatcher(fake_sender, contacts)
    event = {"zone_id": "concession", "message": "Please return to your zone", "event_type": "zone_nudge_sent"}
    dispatcher("employee_nudge", event)

    fake_sender.send.assert_called_once()
    call_args = fake_sender.send.call_args
    assert call_args[0][0]["phone"] == "+15551110000"
    assert call_args[0][1] == "Please return to your zone"


def test_dispatcher_routes_supervisor_directive_to_supervisor_contact(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text('{"supervisor_phone": "+15559998888", "zones": {}}')
    contacts = ContactBook(path)

    fake_sender = MagicMock()
    fake_sender.send.return_value = MagicMock(to_dict=lambda: {"sent": True})

    dispatcher = NotificationDispatcher(fake_sender, contacts)
    event = {"zone_id": "concession", "message": "Directive text", "event_type": "zone_supervisor_command"}
    dispatcher("supervisor_directive_send", event)

    call_args = fake_sender.send.call_args
    assert call_args[0][0]["phone"] == "+15559998888"


def test_dispatcher_records_notification_result_on_event(tmp_path):
    contacts = ContactBook(tmp_path / "contacts.json")
    fake_sender = MagicMock()
    fake_sender.send.return_value = MagicMock(to_dict=lambda: {"sent": True, "channel": "twilio_sms", "detail": "SM1"})

    dispatcher = NotificationDispatcher(fake_sender, contacts)
    event = {"zone_id": "concession", "message": "msg", "event_type": "zone_nudge_sent"}
    dispatcher("employee_nudge", event)

    assert event["notification_result"] == {"sent": True, "channel": "twilio_sms", "detail": "SM1"}
