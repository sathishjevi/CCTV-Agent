"""Unit tests for sms_webhook.py — inbound SMS keyword parsing and
signature validation. The signature check is the ONLY thing standing
between the webhook and forged "employee replies", so its failure modes
(empty token, wrong signature, library error) are tested explicitly, not
just the happy path."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from sms_webhook import parse_sms_command, reply_twiml, validate_signature  # noqa: E402


# ── parse_sms_command ────────────────────────────────────────────────────

def test_parses_bare_keyword():
    assert parse_sms_command("START") == ("START", None)
    assert parse_sms_command("done") == ("DONE", None)


def test_parses_keyword_with_task_code():
    assert parse_sms_command("START ABC123") == ("START", "ABC123")
    assert parse_sms_command("done abc123") == ("DONE", "ABC123")


def test_handles_extra_whitespace():
    assert parse_sms_command("   Start   ABC123   ") == ("START", "ABC123")


def test_recognizes_synonyms():
    assert parse_sms_command("begin") == ("START", None)
    assert parse_sms_command("completed") == ("DONE", None)
    assert parse_sms_command("extend") == ("MORE", None)
    assert parse_sms_command("help") == ("REVIEW", None)


def test_unrecognized_keyword_returns_none():
    assert parse_sms_command("what is this") == (None, None)
    assert parse_sms_command("lol") == (None, None)


def test_empty_body_returns_none():
    assert parse_sms_command("") == (None, None)
    assert parse_sms_command("   ") == (None, None)
    assert parse_sms_command(None) == (None, None)


def test_code_with_punctuation_gets_cleaned():
    assert parse_sms_command("start #ABC-123!") == ("START", "ABC123")


# ── validate_signature ───────────────────────────────────────────────────

def test_validate_signature_rejects_empty_auth_token():
    assert validate_signature("", "https://example.com/webhook", {}, "somesig") is False


def test_validate_signature_rejects_empty_signature():
    assert validate_signature("realtoken", "https://example.com/webhook", {}, "") is False


def test_validate_signature_uses_twilio_validator_correctly():
    fake_validator_instance = MagicMock()
    fake_validator_instance.validate.return_value = True
    fake_validator_cls = MagicMock(return_value=fake_validator_instance)
    fake_module = MagicMock()
    fake_module.RequestValidator = fake_validator_cls

    with patch.dict(sys.modules, {"twilio.request_validator": fake_module}):
        result = validate_signature("mytoken", "https://x/webhook", {"Body": "START"}, "sig123")

    assert result is True
    fake_validator_cls.assert_called_once_with("mytoken")
    fake_validator_instance.validate.assert_called_once_with(
        "https://x/webhook", {"Body": "START"}, "sig123")


def test_validate_signature_returns_false_on_mismatch():
    fake_validator_instance = MagicMock()
    fake_validator_instance.validate.return_value = False
    fake_module = MagicMock()
    fake_module.RequestValidator = MagicMock(return_value=fake_validator_instance)

    with patch.dict(sys.modules, {"twilio.request_validator": fake_module}):
        result = validate_signature("mytoken", "https://x/webhook", {}, "wrong-sig")
    assert result is False


def test_validate_signature_never_raises_on_library_error():
    fake_module = MagicMock()
    fake_module.RequestValidator = MagicMock(side_effect=RuntimeError("boom"))
    with patch.dict(sys.modules, {"twilio.request_validator": fake_module}):
        assert validate_signature("mytoken", "https://x/webhook", {}, "sig") is False


# ── reply_twiml ──────────────────────────────────────────────────────────

def test_reply_twiml_wraps_message():
    xml = reply_twiml("Task started, thanks!")
    assert "<Response>" in xml
    assert "Task started, thanks!" in xml
    assert xml.startswith("<?xml")


def test_reply_twiml_escapes_special_characters():
    xml = reply_twiml('Task <injected> & "quoted"')
    assert "<injected>" not in xml
    assert "&lt;injected&gt;" in xml
    assert "&amp;" in xml
