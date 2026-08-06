"""Unit tests for the pre-commit secret guard's detection logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_no_secrets import _is_env_shaped_file, _is_placeholder, scan_text  # noqa: E402


# ── placeholder detection ────────────────────────────────────────────────

def test_placeholder_empty_string():
    assert _is_placeholder("") is True


def test_placeholder_angle_brackets():
    assert _is_placeholder("<your-key-here>") is True


def test_placeholder_change_me():
    assert _is_placeholder("CHANGE_ME") is True
    assert _is_placeholder("change_me") is True


def test_placeholder_xxx():
    assert _is_placeholder("xxxxxxxx") is True


def test_real_looking_value_is_not_a_placeholder():
    assert _is_placeholder("sk-ant-api03-abc123def456") is False
    assert _is_placeholder("AC1234567890abcdef1234567890abcdef") is False


# ── env-shaped file detection ────────────────────────────────────────────

def test_env_shaped_file_matches_secrets_env():
    assert _is_env_shaped_file("config/secrets.env") is True
    assert _is_env_shaped_file("config/deployment.env") is True
    assert _is_env_shaped_file(".env.local") is True


def test_env_shaped_file_excludes_templates():
    assert _is_env_shaped_file("config/secrets.env.template") is False


def test_env_shaped_file_excludes_python_source():
    assert _is_env_shaped_file("services/floorwatch-rules-engine/app/config.py") is False


# ── scan_text: known-var-name check (env files only) ─────────────────────

def test_scan_text_flags_real_value_in_env_file():
    findings = scan_text("ANTHROPIC_API_KEY=sk-ant-api03-reallooking1234\n", "config/secrets.env")
    assert any("ANTHROPIC_API_KEY" in f for f in findings)


def test_scan_text_does_not_flag_placeholder_in_env_file():
    findings = scan_text("ANTHROPIC_API_KEY=\n", "config/secrets.env")
    assert findings == []


def test_scan_text_does_not_flag_python_source_defining_the_lookup():
    findings = scan_text('ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")\n', "app/config.py")
    assert findings == []


def test_scan_text_does_not_flag_test_fixture_in_python_file():
    findings = scan_text('ANTHROPIC_API_KEY = "sk-fake-key"\n', "tests/test_llm.py")
    assert findings == []  # not env-shaped, and the vendor pattern doesn't match "sk-fake-key" (no sk-ant- prefix)


def test_scan_text_ignores_unrelated_variable_names():
    findings = scan_text("SOME_OTHER_VAR=whatever\n", "config/secrets.env")
    assert findings == []


# ── scan_text: vendor pattern check (runs on any file type) ──────────────

# These three tests need a REALISTIC-SHAPED fake value to test detection
# at all — that value would otherwise permanently self-flag this very
# file every time the guard scans the repo. `# nosecret` is the documented
# inline suppression marker for exactly this case (see check_no_secrets.py).
# It's fed to scan_text() as a raw string here specifically so the marker
# isn't literally present when scan_text() checks THIS line for a finding
# — the assertion below still confirms detection actually fired.

def test_scan_text_flags_twilio_sid_anywhere():
    fixture = "sid = 'AC1234567890abcdef1234567890abcdef'\n"  # nosecret
    findings = scan_text(fixture, "somefile.py")
    assert any("Twilio" in f for f in findings)


def test_scan_text_flags_anthropic_key_shape_anywhere():
    fixture = "key: sk-ant-api03-abcdefghijklmnopqrst\n"  # nosecret
    findings = scan_text(fixture, "notes.md")
    assert any("Anthropic" in f for f in findings)


def test_scan_text_flags_postgres_dsn_with_password():
    fixture = "DATABASE_URL=postgresql://admin:hunter2@db.internal/floorwatch\n"  # nosecret
    findings = scan_text(fixture, "somefile.txt")
    assert any("DSN" in f for f in findings)


def test_scan_text_does_not_flag_postgres_dsn_without_password():
    findings = scan_text("# example: postgresql://localhost/floorwatch\n", "README.md")
    assert findings == []


def test_scan_text_clean_line_produces_no_findings():
    findings = scan_text("This is a perfectly normal log line about coverage gaps.\n", "notes.md")
    assert findings == []


# ── inline suppression marker ────────────────────────────────────────────

def test_nosecret_marker_suppresses_an_otherwise_real_finding():
    without_marker = scan_text("ANTHROPIC_API_KEY=sk-ant-api03-reallooking1234\n", "config/secrets.env")
    assert without_marker  # sanity: would be flagged without the marker

    with_marker = scan_text("ANTHROPIC_API_KEY=sk-ant-api03-reallooking1234  # nosecret\n", "config/secrets.env")
    assert with_marker == []
