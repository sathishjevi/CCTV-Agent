"""Unit tests for the secrets-handling helpers."""

import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from floorwatch_secrets_guard import (  # noqa: E402
    _RedactingStream, check_file_permissions, install_stderr_redaction, load_deployment_config,
)


# ── load_deployment_config ───────────────────────────────────────────────

def test_load_deployment_config_missing_files_does_not_raise(tmp_path):
    load_deployment_config(tmp_path)  # no config/ dir at all — must not raise


def test_load_deployment_config_loads_deployment_env(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOORWATCH_TEST_VAR", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "deployment.env").write_text("FLOORWATCH_TEST_VAR=from_file\n")

    load_deployment_config(tmp_path)

    import os
    assert os.environ.get("FLOORWATCH_TEST_VAR") == "from_file"
    monkeypatch.delenv("FLOORWATCH_TEST_VAR", raising=False)


def test_load_deployment_config_never_overrides_real_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOORWATCH_TEST_VAR", "real_env_value")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "deployment.env").write_text("FLOORWATCH_TEST_VAR=from_file\n")

    load_deployment_config(tmp_path)

    import os
    assert os.environ.get("FLOORWATCH_TEST_VAR") == "real_env_value"


# ── check_file_permissions ───────────────────────────────────────────────

def test_check_file_permissions_missing_file_returns_none(tmp_path):
    assert check_file_permissions(tmp_path / "nonexistent") is None


def test_check_file_permissions_never_raises_on_a_real_file(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("FLOORWATCH_TWILIO_AUTH_TOKEN=abc123\n")
    # Just assert it doesn't raise — the actual warn/no-warn outcome is
    # platform- and filesystem-dependent (NTFS ACLs vs POSIX mode bits).
    check_file_permissions(p)


def test_check_posix_mode_warns_on_world_readable_file(tmp_path):
    if sys.platform == "win32":
        return  # POSIX-only check; Windows path exercised by the "never raises" test above
    p = tmp_path / "secrets.env"
    p.write_text("SECRET=abc123\n")
    p.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)  # world-readable
    messages = []
    check_file_permissions(p, log=messages.append)
    assert any("readable" in m for m in messages)


def test_check_posix_mode_silent_on_owner_only_file(tmp_path):
    if sys.platform == "win32":
        return
    p = tmp_path / "secrets.env"
    p.write_text("SECRET=abc123\n")
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner-only
    messages = []
    check_file_permissions(p, log=messages.append)
    assert messages == []


# ── install_stderr_redaction / _RedactingStream ──────────────────────────

def test_redacting_stream_scrubs_known_secret():
    import io
    buf = io.StringIO()
    stream = _RedactingStream(buf, ["supersecrettoken123"])
    stream.write("sent SMS using token supersecrettoken123 successfully")
    assert "supersecrettoken123" not in buf.getvalue()
    assert "***REDACTED***" in buf.getvalue()


def test_redacting_stream_ignores_short_values_to_avoid_over_redaction():
    import io
    buf = io.StringIO()
    stream = _RedactingStream(buf, ["ab", ""])  # too short / empty — must not redact everything
    stream.write("this is a normal log line about ab testing")
    assert buf.getvalue() == "this is a normal log line about ab testing"


def test_redacting_stream_handles_multiple_secrets():
    import io
    buf = io.StringIO()
    stream = _RedactingStream(buf, ["secretone123456", "secrettwo654321"])
    stream.write("secretone123456 and secrettwo654321 both appear here")
    out = buf.getvalue()
    assert "secretone123456" not in out
    assert "secrettwo654321" not in out
    assert out.count("***REDACTED***") == 2


def test_install_stderr_redaction_is_idempotent(monkeypatch):
    import io
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    install_stderr_redaction(["firstsecret123"])
    first_wrapper = sys.stderr
    install_stderr_redaction(["secondsecret456"])

    assert sys.stderr is first_wrapper  # not double-wrapped
    print("firstsecret123 and secondsecret456", file=sys.stderr)
    assert "firstsecret123" not in fake_stderr.getvalue()
    assert "secondsecret456" not in fake_stderr.getvalue()
