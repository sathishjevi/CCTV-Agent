"""Unit tests for employee_directory.py — the assignee side of the task
workflow. Same mocked-Postgres + real-JSON-fallback test pattern as
test_event_history.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from employee_directory import (  # noqa: E402
    JsonEmployeeDirectory, PostgresEmployeeDirectory, build_employee_directory,
    normalize_phone, validate_channel, validate_employee_number, validate_phone,
    validate_primary_contact,
)


# ── validation helpers ──────────────────────────────────────────────────

def test_validate_employee_number_accepts_simple_numbers():
    assert validate_employee_number("101")[0] is True
    assert validate_employee_number("EMP-101")[0] is True


def test_validate_employee_number_rejects_empty_and_too_long():
    assert validate_employee_number("")[0] is False
    assert validate_employee_number("x" * 21)[0] is False


def test_validate_employee_number_rejects_special_characters():
    assert validate_employee_number('101"><script>')[0] is False
    assert validate_employee_number("101 102")[0] is False


def test_validate_phone_accepts_common_formats():
    assert validate_phone("+15551234567")[0] is True
    assert validate_phone("555-123-4567")[0] is True
    assert validate_phone("(555) 123 4567")[0] is True


def test_validate_phone_rejects_garbage():
    assert validate_phone("")[0] is False
    assert validate_phone("not-a-phone")[0] is False
    assert validate_phone("123")[0] is False  # too short


def test_normalize_phone_strips_formatting():
    assert normalize_phone("(555) 123-4567") == "5551234567"
    assert normalize_phone("+1 555 123 4567") == "+15551234567"


# ── Feature 1/2 validation helpers ───────────────────────────────────────

def test_validate_channel_accepts_none_and_empty():
    assert validate_channel(None)[0] is True
    assert validate_channel("")[0] is True


def test_validate_channel_accepts_sms_and_fcm():
    assert validate_channel("sms")[0] is True
    assert validate_channel("fcm")[0] is True


def test_validate_channel_rejects_unknown_value():
    ok, reason = validate_channel("pager")
    assert ok is False
    assert "sms" in reason and "fcm" in reason


def test_validate_primary_contact_allows_supervisor():
    assert validate_primary_contact("supervisor", True)[0] is True
    assert validate_primary_contact("supervisor", False)[0] is True


def test_validate_primary_contact_rejects_employee_role():
    ok, reason = validate_primary_contact("employee", True)
    assert ok is False
    assert "supervisor" in reason


def test_validate_primary_contact_false_is_always_fine():
    # is_primary_contact=False never triggers the role check, regardless of role
    assert validate_primary_contact("employee", False)[0] is True


# ── JsonEmployeeDirectory ───────────────────────────────────────────────

def test_json_add_and_get_roundtrips(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat Smith", "employee", "janitorial", "+15551234567")
    entry = d.get("101")
    assert entry["name"] == "Pat Smith"
    assert entry["department"] == "janitorial"
    assert entry["phone"] == "+15551234567"
    assert entry["active"] is True


def test_json_add_normalizes_phone(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "(555) 123-4567")
    assert d.get("101")["phone"] == "5551234567"


def test_json_add_rejects_invalid_role(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    with pytest.raises(ValueError):
        d.add("101", "Pat", "manager", "janitorial", "+15551234567")


def test_json_add_same_number_updates_but_preserves_active_flag(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+15551234567")
    d.set_active("101", False)
    d.add("101", "Pat Smith-Jones", "employee", "concession", "+15559998888")
    entry = d.get("101")
    assert entry["name"] == "Pat Smith-Jones"
    assert entry["department"] == "concession"
    assert entry["active"] is False  # deactivation not silently undone by an edit


def test_json_get_by_phone_matches_normalized(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+1 555 123 4567")
    assert d.get_by_phone("+15551234567")["employee_number"] == "101"


def test_json_get_by_phone_ignores_inactive(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+15551234567")
    d.set_active("101", False)
    assert d.get_by_phone("+15551234567") is None


def test_json_get_by_phone_unknown_returns_none(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    assert d.get_by_phone("+19998887777") is None


def test_json_list_all_filters_by_department_and_active(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+15551111111")
    d.add("102", "Sam", "employee", "concession", "+15552222222")
    d.add("103", "Ali", "supervisor", "janitorial", "+15553333333")
    d.set_active("103", False)

    assert [e["employee_number"] for e in d.list_all()] == ["101", "102", "103"]
    assert [e["employee_number"] for e in d.list_all(department="janitorial")] == ["101", "103"]
    assert [e["employee_number"] for e in d.list_all(department="janitorial", active_only=True)] == ["101"]


def test_json_set_active_unknown_returns_false(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    assert d.set_active("nobody", False) is False


def test_json_add_stores_channel_and_fcm_token(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+15551234567", channel="fcm", fcm_token="tok-abc123")
    entry = d.get("101")
    assert entry["channel"] == "fcm"
    assert entry["fcm_token"] == "tok-abc123"


def test_json_add_defaults_channel_to_none(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("101", "Pat", "employee", "janitorial", "+15551234567")
    assert d.get("101")["channel"] is None


def test_json_add_rejects_invalid_channel(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    with pytest.raises(ValueError):
        d.add("101", "Pat", "employee", "janitorial", "+15551234567", channel="pager")


def test_json_add_accepts_primary_contact_on_supervisor(tmp_path):
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    d.add("900", "Jordan Lee", "supervisor", "janitorial", "+15559990000", is_primary_contact=True)
    assert d.get("900")["is_primary_contact"] is True


def test_json_add_rejects_primary_contact_on_employee(tmp_path):
    """The primary contact for a department must always be a supervisor
    — never a line employee. Rejected at write time, not silently
    allowed, since a line employee ending up flagged primary would mean
    auto-assignment could route detection-triggered work straight to
    them, exactly what the supervisor-only design is meant to prevent."""
    d = JsonEmployeeDirectory(tmp_path / "employees.json")
    with pytest.raises(ValueError):
        d.add("101", "Pat", "employee", "janitorial", "+15551234567", is_primary_contact=True)


# ── PostgresEmployeeDirectory (mocked — no real Postgres in this sandbox) ──

def _fake_psycopg_module(fake_conn):
    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value.__enter__ = MagicMock(return_value=fake_conn)
    fake_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
    return fake_psycopg


def test_postgres_schema_and_indexes_created_on_init():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresEmployeeDirectory("postgresql://fake/dsn")

    all_sql = " ".join(call.args[0] for call in fake_conn.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS floorwatch_employees" in all_sql
    assert "CREATE INDEX" in all_sql


def test_postgres_add_runs_upsert_with_normalized_phone():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        d = PostgresEmployeeDirectory("postgresql://fake/dsn")
        d.add("101", "Pat", "employee", "janitorial", "(555) 123-4567", created_by="admin")

    call_args = fake_conn.execute.call_args_list[-1]
    assert "INSERT INTO floorwatch_employees" in call_args[0][0]
    assert "ON CONFLICT (employee_number) DO UPDATE" in call_args[0][0]
    assert call_args[0][1][4] == "5551234567"  # phone position, normalized


def test_postgres_add_rejects_invalid_role():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        d = PostgresEmployeeDirectory("postgresql://fake/dsn")
        with pytest.raises(ValueError):
            d.add("101", "Pat", "root", "janitorial", "+15551234567")


def test_postgres_migration_adds_new_columns():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        PostgresEmployeeDirectory("postgresql://fake/dsn")

    all_sql = " ".join(call.args[0] for call in fake_conn.execute.call_args_list)
    assert "ADD COLUMN IF NOT EXISTS channel" in all_sql
    assert "ADD COLUMN IF NOT EXISTS fcm_token" in all_sql
    assert "ADD COLUMN IF NOT EXISTS is_primary_contact" in all_sql


def test_postgres_add_rejects_invalid_channel():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        d = PostgresEmployeeDirectory("postgresql://fake/dsn")
        with pytest.raises(ValueError):
            d.add("101", "Pat", "employee", "janitorial", "+15551234567", channel="pager")


def test_postgres_add_rejects_primary_contact_on_employee():
    fake_conn = MagicMock()
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        d = PostgresEmployeeDirectory("postgresql://fake/dsn")
        with pytest.raises(ValueError):
            d.add("101", "Pat", "employee", "janitorial", "+15551234567", is_primary_contact=True)


def test_postgres_get_by_phone_normalizes_lookup():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    with patch.dict(sys.modules, {"psycopg": _fake_psycopg_module(fake_conn)}):
        d = PostgresEmployeeDirectory("postgresql://fake/dsn")
        d.get_by_phone("(555) 123-4567")

    call_args = fake_conn.execute.call_args_list[-1]
    assert call_args[0][1] == ("5551234567",)


# ── build_employee_directory dispatch ────────────────────────────────────

def test_build_uses_json_fallback_when_no_dsn(tmp_path):
    d = build_employee_directory(None, tmp_path / "employees.json")
    assert isinstance(d, JsonEmployeeDirectory)


def test_build_falls_back_on_postgres_failure(tmp_path):
    with patch("employee_directory.PostgresEmployeeDirectory", side_effect=Exception("refused")):
        d = build_employee_directory("postgresql://unreachable/dsn", tmp_path / "employees.json")
    assert isinstance(d, JsonEmployeeDirectory)


def test_build_uses_postgres_when_reachable(tmp_path):
    fake = MagicMock()
    with patch("employee_directory.PostgresEmployeeDirectory", return_value=fake):
        d = build_employee_directory("postgresql://fake/dsn", tmp_path / "employees.json")
    assert d is fake
