"""Unit tests for the go-live checklist's individual check functions."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from go_live_checklist import (  # noqa: E402
    check_auth_accounts_exist, check_auth_enforced_live, check_contacts_configured,
    check_false_positive_rate, check_notification_channel, check_roster_active,
)


# ── false-positive rate ────────────────────────────────────────────────

def test_fp_rate_fails_when_report_missing(tmp_path):
    result = check_false_positive_rate(tmp_path / "nonexistent.json", 0.15)
    assert result.passed is False
    assert "No accuracy report" in result.detail


def test_fp_rate_fails_when_report_not_json(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("not json{{{")
    result = check_false_positive_rate(path, 0.15)
    assert result.passed is False


def test_fp_rate_fails_with_zero_reviewed_samples(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"false_positive_rate": None, "total_reviewed": 0}))
    result = check_false_positive_rate(path, 0.15)
    assert result.passed is False
    assert "No reviewed samples" in result.detail


def test_fp_rate_fails_with_too_few_reviewed_samples(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"false_positive_rate": 0.0, "total_reviewed": 3}))
    result = check_false_positive_rate(path, 0.15)
    assert result.passed is False
    assert "too few" in result.detail.lower()


def test_fp_rate_passes_below_target(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"false_positive_rate": 0.10, "total_reviewed": 20}))
    result = check_false_positive_rate(path, 0.15)
    assert result.passed is True


def test_fp_rate_fails_above_target(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"false_positive_rate": 0.30, "total_reviewed": 20}))
    result = check_false_positive_rate(path, 0.15)
    assert result.passed is False


# ── roster ────────────────────────────────────────────────────────────

def test_roster_fails_when_missing(tmp_path):
    result = check_roster_active(tmp_path / "nonexistent.json")
    assert result.passed is False


def test_roster_fails_when_empty(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"_comment": "nothing here"}))
    result = check_roster_active(path)
    assert result.passed is False
    assert "no zone entries" in result.detail.lower()


def test_roster_passes_with_real_entries(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"_comment": "x", "concession": True, "lobby": False}))
    result = check_roster_active(path)
    assert result.passed is True
    assert "2 zone" in result.detail


# ── contacts ──────────────────────────────────────────────────────────

def test_contacts_fails_when_staffed_zone_missing_contact(tmp_path):
    roster_path = tmp_path / "roster.json"
    contacts_path = tmp_path / "contacts.json"
    roster_path.write_text(json.dumps({"concession": True, "lobby": True}))
    contacts_path.write_text(json.dumps({"zones": {"concession": {"employee_phone": "+15551234567"}}}))
    result = check_contacts_configured(contacts_path, roster_path)
    assert result.passed is False
    assert "lobby" in result.detail


def test_contacts_passes_when_all_staffed_zones_covered(tmp_path):
    roster_path = tmp_path / "roster.json"
    contacts_path = tmp_path / "contacts.json"
    roster_path.write_text(json.dumps({"concession": True}))
    contacts_path.write_text(json.dumps({"zones": {"concession": {"employee_phone": "+15551234567"}}}))
    result = check_contacts_configured(contacts_path, roster_path)
    assert result.passed is True


def test_contacts_ignores_unstaffed_zones():
    # An unstaffed zone missing a contact should not fail the check.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        roster_path = Path(d) / "roster.json"
        contacts_path = Path(d) / "contacts.json"
        roster_path.write_text(json.dumps({"concession": True, "lobby": False}))
        contacts_path.write_text(json.dumps({"zones": {"concession": {"employee_phone": "+15551234567"}}}))
        result = check_contacts_configured(contacts_path, roster_path)
        assert result.passed is True


# ── notification channel ─────────────────────────────────────────────

class FakeConfig:
    NOTIFY_CHANNEL = "none"
    TWILIO_ACCOUNT_SID = ""
    TWILIO_AUTH_TOKEN = ""
    TWILIO_FROM_NUMBER = ""
    FCM_CREDENTIALS_PATH = ""


def test_notify_channel_fails_when_none():
    cfg = FakeConfig()
    result = check_notification_channel(cfg)
    assert result.passed is False


def test_notify_channel_twilio_fails_when_credentials_missing():
    cfg = FakeConfig()
    cfg.NOTIFY_CHANNEL = "twilio"
    result = check_notification_channel(cfg)
    assert result.passed is False
    assert "not fully set" in result.detail


def test_notify_channel_twilio_passes_with_mocked_valid_credentials():
    cfg = FakeConfig()
    cfg.NOTIFY_CHANNEL = "twilio"
    cfg.TWILIO_ACCOUNT_SID = "ACxxxx"
    cfg.TWILIO_AUTH_TOKEN = "authtoken"
    cfg.TWILIO_FROM_NUMBER = "+15550009999"

    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.api.accounts.return_value.fetch.return_value = MagicMock(status="active")
        result = check_notification_channel(cfg)
        assert result.passed is True
        assert "active" in result.detail


def test_notify_channel_twilio_fails_when_credentials_invalid():
    cfg = FakeConfig()
    cfg.NOTIFY_CHANNEL = "twilio"
    cfg.TWILIO_ACCOUNT_SID = "ACxxxx"
    cfg.TWILIO_AUTH_TOKEN = "badtoken"
    cfg.TWILIO_FROM_NUMBER = "+15550009999"

    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.api.accounts.return_value.fetch.side_effect = Exception("Authentication error")
        result = check_notification_channel(cfg)
        assert result.passed is False
        assert "Authentication error" in result.detail


def test_notify_channel_fcm_fails_when_credentials_file_missing(tmp_path):
    cfg = FakeConfig()
    cfg.NOTIFY_CHANNEL = "fcm"
    cfg.FCM_CREDENTIALS_PATH = str(tmp_path / "nonexistent.json")
    result = check_notification_channel(cfg)
    assert result.passed is False
    assert "not found" in result.detail


def test_notify_channel_unrecognized_fails():
    cfg = FakeConfig()
    cfg.NOTIFY_CHANNEL = "carrier_pigeon"
    result = check_notification_channel(cfg)
    assert result.passed is False


# ── authentication (SECURITY_REVIEW.md M3) ──────────────────────────────

def test_auth_accounts_fails_when_users_file_missing(tmp_path):
    result = check_auth_accounts_exist(tmp_path / "nonexistent_users.json")
    assert result.passed is False
    assert "create_user.py" in result.detail


def test_auth_accounts_fails_when_file_not_json(tmp_path):
    p = tmp_path / "users.json"
    p.write_text("not json")
    result = check_auth_accounts_exist(p)
    assert result.passed is False


def test_auth_accounts_fails_when_empty(tmp_path):
    p = tmp_path / "users.json"
    p.write_text("{}")
    result = check_auth_accounts_exist(p)
    assert result.passed is False
    assert "no accounts" in result.detail


def test_auth_accounts_passes_with_real_users(tmp_path):
    p = tmp_path / "users.json"
    p.write_text(json.dumps({"alice": {"password_hash": "...", "role": "supervisor"}}))
    result = check_auth_accounts_exist(p)
    assert result.passed is True
    assert "1 account" in result.detail


def test_auth_enforced_live_fails_when_service_unreachable():
    result = check_auth_enforced_live("http://127.0.0.1:1")  # nothing listens here
    assert result.passed is False
    assert "Could not reach" in result.detail


def test_auth_enforced_live_passes_on_401():
    mock_response = MagicMock(status_code=401)
    with patch("go_live_checklist.httpx.get", return_value=mock_response):
        result = check_auth_enforced_live("http://127.0.0.1:8080")
    assert result.passed is True


def test_auth_enforced_live_fails_when_endpoint_open():
    mock_response = MagicMock(status_code=200)
    with patch("go_live_checklist.httpx.get", return_value=mock_response):
        result = check_auth_enforced_live("http://127.0.0.1:8080")
    assert result.passed is False
    assert "NOT protected" in result.detail
