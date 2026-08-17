"""Unit tests for the shared structured-logging module (used by both
floorwatch-rules-engine and floorwatch-intelligence's always-running
FastAPI services — see floorwatch_logging.py's module docstring)."""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from floorwatch_logging import get_logger  # noqa: E402


def test_log_emits_one_json_line_to_stderr(capsys):
    log = get_logger("rules-engine")
    log("service started")
    captured = capsys.readouterr()
    assert captured.out == ""  # goes to stderr, not stdout
    record = json.loads(captured.err.strip())
    assert record["message"] == "service started"


def test_log_includes_service_level_timestamp_message(capsys):
    log = get_logger("rules-engine.cluster_bus")
    log("consuming command stream")
    record = json.loads(capsys.readouterr().err.strip())
    assert record["service"] == "rules-engine.cluster_bus"
    assert record["level"] == "info"  # default
    assert record["message"] == "consuming command stream"
    # timestamp is a real, parseable ISO-8601 value
    datetime.fromisoformat(record["timestamp"])


def test_log_accepts_explicit_level(capsys):
    log = get_logger("rules-engine")
    log("could not create consumer group: boom", level="warning")
    record = json.loads(capsys.readouterr().err.strip())
    assert record["level"] == "warning"
    assert "WARNING" not in record["message"]  # no redundant text prefix needed


def test_log_extra_kwargs_become_top_level_fields(capsys):
    log = get_logger("rules-engine")
    log("deactivated account", username="alice", admin="bob")
    record = json.loads(capsys.readouterr().err.strip())
    assert record["username"] == "alice"
    assert record["admin"] == "bob"
    assert record["message"] == "deactivated account"


def test_two_loggers_have_independent_service_tags(capsys):
    log_a = get_logger("rules-engine")
    log_b = get_logger("intelligence")
    log_a("hello from a")
    log_b("hello from b")
    lines = capsys.readouterr().err.strip().splitlines()
    record_a, record_b = json.loads(lines[0]), json.loads(lines[1])
    assert record_a["service"] == "rules-engine"
    assert record_b["service"] == "intelligence"


def test_each_call_is_valid_standalone_json_not_a_shared_buffer(capsys):
    log = get_logger("rules-engine")
    log("first")
    log("second")
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"
    assert json.loads(lines[1])["message"] == "second"
