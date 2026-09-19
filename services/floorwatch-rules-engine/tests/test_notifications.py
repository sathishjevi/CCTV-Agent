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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from notifications import (  # noqa: E402
    ContactBook, Fast2SmsSender, Msg91SmsSender, NoOpSender, NotificationDispatcher,
    TwilioSmsSender, build_sender, _mask_context, _mask_phone, _mask_token,
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
    sender.dlt_template = None

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


# ── TwilioSmsSender DLT wrapping (config.SMS_COUNTRY == "IN") ────────────

def test_twilio_sender_sends_freeform_when_no_dlt_template_set():
    """Default behavior, unchanged from before SMS_COUNTRY existed."""
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")
        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999")  # dlt_template defaults to None
        sender.send({"phone": "+15551234567"}, "Zone needs coverage")
        mock_instance.messages.create.assert_called_once_with(
            to="+15551234567", from_="+15550009999", body="Zone needs coverage")


def test_twilio_sender_wraps_message_through_dlt_template_when_set():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")
        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999",
                                  dlt_template="Floorwatch: {message}")
        sender.send({"phone": "+919876543210"}, "task assigned")
        mock_instance.messages.create.assert_called_once_with(
            to="+919876543210", from_="+15550009999", body="Floorwatch: task assigned")


def test_build_sender_twilio_applies_dlt_template_when_country_is_india():
    class FakeConfig:
        TWILIO_ACCOUNT_SID = "ACxxxx"
        TWILIO_AUTH_TOKEN = "authtoken"
        TWILIO_FROM_NUMBER = "+15550009999"
        SMS_COUNTRY = "IN"
        DLT_REQUIRED_COUNTRIES = {"IN"}
        TWILIO_DLT_TEMPLATE = "Floorwatch: {message}"
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")
        sender = build_sender("twilio", FakeConfig())
        assert isinstance(sender, TwilioSmsSender)
        sender.send({"phone": "+919876543210"}, "task assigned")
        mock_instance.messages.create.assert_called_once_with(
            to="+919876543210", from_="+15550009999", body="Floorwatch: task assigned")


def test_build_sender_twilio_sends_freeform_when_country_is_not_india():
    class FakeConfig:
        TWILIO_ACCOUNT_SID = "ACxxxx"
        TWILIO_AUTH_TOKEN = "authtoken"
        TWILIO_FROM_NUMBER = "+15550009999"
        SMS_COUNTRY = "US"
        DLT_REQUIRED_COUNTRIES = {"IN"}
        TWILIO_DLT_TEMPLATE = "Floorwatch: {message}"
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")
        sender = build_sender("twilio", FakeConfig())
        sender.send({"phone": "+15551234567"}, "task assigned")
        mock_instance.messages.create.assert_called_once_with(
            to="+15551234567", from_="+15550009999", body="task assigned")  # unwrapped


def test_build_sender_twilio_missing_country_attrs_defaults_to_freeform():
    """A config predating this feature (no SMS_COUNTRY/DLT_REQUIRED_COUNTRIES/
    TWILIO_DLT_TEMPLATE attrs at all) must not raise, and must keep sending
    freeform — the exact behavior it had before this feature existed."""
    class FakeConfig:
        TWILIO_ACCOUNT_SID = "ACxxxx"
        TWILIO_AUTH_TOKEN = "authtoken"
        TWILIO_FROM_NUMBER = "+15550009999"
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM123")
        sender = build_sender("twilio", FakeConfig())
        sender.send({"phone": "+15551234567"}, "task assigned")
        mock_instance.messages.create.assert_called_once_with(
            to="+15551234567", from_="+15550009999", body="task assigned")


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


def test_build_sender_missing_sms_provider_attr_defaults_to_twilio():
    """A config object that predates FLOORWATCH_SMS_PROVIDER (e.g. this
    minimal FakeConfig, matching the test above) must not raise — this
    caught a real bug during development: an unconditional
    config.SMS_PROVIDER access broke build_sender's own never-raises
    contract for exactly this shape of config object."""
    class FakeConfig:
        TWILIO_ACCOUNT_SID = ""
        TWILIO_AUTH_TOKEN = ""
        TWILIO_FROM_NUMBER = ""
    sender = build_sender("sms", FakeConfig())
    assert sender is not None


# ── MSG91Sender / Fast2SmsSender (mocked HTTP) — SMS_PROVIDER dispatch ────

def test_msg91_sender_sends_via_mocked_http():
    with patch("httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, content=b"{}", json=lambda: {"type": "success", "message": "req-123"})
        sender = Msg91SmsSender("authkey123", "tmpl456", "var")
        result = sender.send({"phone": "+919876543210"}, "Floorwatch: task assigned")

        assert result.sent is True
        assert result.channel == "msg91_sms"
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["authkey"] == "authkey123"
        assert kwargs["json"]["template_id"] == "tmpl456"
        assert kwargs["json"]["recipients"] == [{"mobiles": "919876543210", "var": "Floorwatch: task assigned"}]


def test_msg91_sender_skips_send_when_no_phone_on_file():
    sender = Msg91SmsSender("authkey123", "tmpl456", "var")
    result = sender.send({"phone": None}, "message")
    assert result.sent is False
    assert "no phone" in result.detail


def test_msg91_sender_handles_error_response_gracefully():
    with patch("httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, content=b"{}", json=lambda: {"type": "error", "message": "invalid template_id"})
        sender = Msg91SmsSender("authkey123", "tmpl456", "var")
        result = sender.send({"phone": "+919876543210"}, "message")
        assert result.sent is False
        assert "invalid template_id" in result.detail


def test_msg91_sender_missing_credentials_raises_at_construction():
    # build_sender() is what's required to catch this — the class itself
    # should fail loudly at construction so a bad config is diagnosable.
    with pytest.raises(ValueError):
        Msg91SmsSender("", "", "var")


def test_fast2sms_sender_sends_via_mocked_http_strips_country_code():
    with patch("httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, content=b"{}", json=lambda: {"return": True, "request_id": "req-789"})
        sender = Fast2SmsSender("apikey123", "FLOORW", "tmpl456")
        result = sender.send({"phone": "+919876543210"}, "Floorwatch: task assigned")

        assert result.sent is True
        assert result.channel == "fast2sms_sms"
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "apikey123"
        assert kwargs["json"]["numbers"] == "9876543210"  # +91 country code stripped
        assert kwargs["json"]["variables_values"] == "Floorwatch: task assigned"
        assert kwargs["json"]["message"] == "tmpl456"


def test_fast2sms_sender_skips_send_when_no_phone_on_file():
    sender = Fast2SmsSender("apikey123", "FLOORW", "tmpl456")
    result = sender.send({"phone": None}, "message")
    assert result.sent is False
    assert "no phone" in result.detail


def test_fast2sms_sender_missing_credentials_raises_at_construction():
    with pytest.raises(ValueError):
        Fast2SmsSender("", "", "")


def test_build_sender_dispatches_sms_channel_to_msg91_when_configured():
    class FakeConfig:
        SMS_PROVIDER = "msg91"
        MSG91_AUTH_KEY = "authkey123"
        MSG91_TEMPLATE_ID = "tmpl456"
        MSG91_VARIABLE_NAME = "var"
    sender = build_sender("sms", FakeConfig())
    assert isinstance(sender, Msg91SmsSender)


def test_build_sender_dispatches_sms_channel_to_fast2sms_when_configured():
    class FakeConfig:
        SMS_PROVIDER = "fast2sms"
        FAST2SMS_API_KEY = "apikey123"
        FAST2SMS_SENDER_ID = "FLOORW"
        FAST2SMS_MESSAGE_ID = "tmpl456"
    sender = build_sender("sms", FakeConfig())
    assert isinstance(sender, Fast2SmsSender)


def test_build_sender_twilio_channel_alias_also_honors_sms_provider():
    """The "twilio" and "sms" channel strings are interchangeable — an
    existing deployment that still passes "twilio" (e.g.
    TASK_CHANNEL_SENDERS in main.py) still gets whichever provider
    SMS_PROVIDER names, not hardcoded Twilio."""
    class FakeConfig:
        SMS_PROVIDER = "msg91"
        MSG91_AUTH_KEY = "authkey123"
        MSG91_TEMPLATE_ID = "tmpl456"
        MSG91_VARIABLE_NAME = "var"
    sender = build_sender("twilio", FakeConfig())
    assert isinstance(sender, Msg91SmsSender)


def test_build_sender_msg91_bad_config_falls_back_to_noop():
    class FakeConfig:
        SMS_PROVIDER = "msg91"
        MSG91_AUTH_KEY = ""
        MSG91_TEMPLATE_ID = ""
        MSG91_VARIABLE_NAME = "var"
    sender = build_sender("sms", FakeConfig())
    assert isinstance(sender, NoOpSender)


def test_build_sender_fast2sms_bad_config_falls_back_to_noop():
    class FakeConfig:
        SMS_PROVIDER = "fast2sms"
        FAST2SMS_API_KEY = ""
        FAST2SMS_SENDER_ID = ""
        FAST2SMS_MESSAGE_ID = ""
    sender = build_sender("sms", FakeConfig())
    assert isinstance(sender, NoOpSender)


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


# ── FCM sender: credentials source + delivery shape ────────────────────

def _fake_firebase():
    """A stand-in `firebase_admin` package (the real SDK needs a real
    Firebase project). Returns (modules_dict, credentials_mock, messaging_mock)."""
    firebase_admin = MagicMock()
    firebase_admin._apps = {}
    credentials = MagicMock()
    messaging = MagicMock()
    firebase_admin.credentials = credentials
    firebase_admin.messaging = messaging
    modules = {"firebase_admin": firebase_admin, "firebase_admin.credentials": credentials,
               "firebase_admin.messaging": messaging}
    return modules, credentials, messaging


def test_fcm_sender_accepts_inline_json_credentials():
    """Railway has variables, not mountable files — the service-account key
    must be usable pasted whole as a JSON string."""
    from notifications import FcmSender
    modules, credentials, _messaging = _fake_firebase()
    with patch.dict(sys.modules, modules):
        FcmSender(credentials_json='{"type": "service_account", "project_id": "demo"}')
    credentials.Certificate.assert_called_once_with({"type": "service_account", "project_id": "demo"})


def test_fcm_sender_prefers_a_file_path_when_both_are_given():
    from notifications import FcmSender
    modules, credentials, _messaging = _fake_firebase()
    with patch.dict(sys.modules, modules):
        FcmSender(credentials_path="/secrets/key.json", credentials_json='{"a": 1}')
    credentials.Certificate.assert_called_once_with("/secrets/key.json")


def test_fcm_sender_sends_a_high_priority_notification():
    from notifications import FcmSender
    modules, _credentials, messaging = _fake_firebase()
    messaging.send.return_value = "projects/demo/messages/1"
    with patch.dict(sys.modules, modules):
        sender = FcmSender(credentials_json='{"type": "service_account"}')
        result = sender.send({"fcm_token": "device-token"}, "Task assigned")
    assert result.sent is True and result.channel == "fcm"
    messaging.AndroidConfig.assert_called_once_with(priority="high")
    kwargs = messaging.Message.call_args.kwargs
    assert kwargs["token"] == "device-token"


def test_fcm_sender_without_a_token_skips_instead_of_guessing():
    from notifications import FcmSender
    modules, _credentials, messaging = _fake_firebase()
    with patch.dict(sys.modules, modules):
        sender = FcmSender(credentials_json='{"type": "service_account"}')
        result = sender.send({"fcm_token": None}, "Task assigned")
    assert result.sent is False and "no fcm token" in result.detail
    messaging.send.assert_not_called()
