"""Error monitoring: off without a DSN, error-level logs reach the reporter
hook, secrets are scrubbed, and the background loops don't die silently."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_logging  # noqa: E402
from error_monitoring import init_error_monitoring, scrub, _before_send  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_reporter():
    yield
    floorwatch_logging.set_error_reporter(None)


def test_monitoring_is_off_without_a_dsn():
    assert init_error_monitoring("") is False
    assert floorwatch_logging._error_reporter is None


def test_missing_sdk_degrades_quietly():
    with patch.dict(sys.modules, {"sentry_sdk": None}):
        assert init_error_monitoring("https://key@example.ingest.sentry.io/1") is False


def test_bad_dsn_never_raises():
    fake = MagicMock()
    fake.init.side_effect = ValueError("Unsupported scheme")
    with patch.dict(sys.modules, {"sentry_sdk": fake}):
        assert init_error_monitoring("not-a-dsn") is False


def test_error_level_logs_reach_the_reporter_but_lower_levels_do_not():
    seen = []
    floorwatch_logging.set_error_reporter(lambda service, message, fields: seen.append((service, message)))
    log = floorwatch_logging.get_logger("rules-engine.test")

    log("just information")
    log("something odd", level="warning")
    log("push delivery failed", level="error")

    assert seen == [("rules-engine.test", "push delivery failed")]


def test_a_broken_reporter_never_breaks_logging():
    def boom(*_a):
        raise RuntimeError("monitoring backend down")

    floorwatch_logging.set_error_reporter(boom)
    floorwatch_logging.get_logger("t")("still logs", level="error")  # must not raise


def test_init_wires_the_reporter_and_disables_pii():
    fake = MagicMock()
    with patch.dict(sys.modules, {"sentry_sdk": fake}):
        assert init_error_monitoring("https://key@example.ingest.sentry.io/1", environment="prod", release="abc") is True
    kwargs = fake.init.call_args.kwargs
    assert kwargs["send_default_pii"] is False
    assert kwargs["traces_sample_rate"] == 0.0
    assert kwargs["environment"] == "prod" and kwargs["release"] == "abc"
    assert floorwatch_logging._error_reporter is not None


def test_tokens_are_scrubbed_from_messages():
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMDEifQ.abcdefghijklmnop"
    assert jwt_like not in scrub(f"failed for token {jwt_like}")
    assert "secret123" not in scrub("Authorization: Bearer secret123")


def test_before_send_strips_auth_headers_bodies_and_cookies():
    event = {"message": "x", "request": {
        "headers": {"Authorization": "Bearer abc", "User-Agent": "ok"},
        "cookies": {"a": "b"}, "data": {"password": "hunter2"}}}
    out = _before_send(event, {})
    assert out["request"]["headers"]["Authorization"] == "[redacted]"
    assert out["request"]["headers"]["User-Agent"] == "ok"
    assert "cookies" not in out["request"] and "data" not in out["request"]


def test_tick_loop_survives_a_failing_tick_and_reports_it():
    """One bad tick used to end the loop for good, silently stopping
    coverage monitoring until the next restart."""
    import main as main_module
    calls = {"n": 0}

    async def flaky_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")

    seen = []
    floorwatch_logging.set_error_reporter(lambda s, m, f: seen.append(m))

    async def run():
        with patch.object(main_module, "_tick_once", flaky_tick), \
             patch.object(main_module.config, "TICK_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(main_module.tick_loop())
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert calls["n"] >= 2, "loop stopped after the first failure"
    assert any("tick failed" in m for m in seen)


def test_a_crashed_background_task_is_reported():
    import main as main_module
    seen = []
    floorwatch_logging.set_error_reporter(lambda s, m, f: seen.append(m))

    async def run():
        async def dies():
            raise ValueError("boom")
        t = asyncio.create_task(dies())
        t.add_done_callback(main_module._report_if_crashed)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert any("crashed" in m and "boom" in m for m in seen)
