"""Error monitoring (PostHog): off without a key, error-level logs reach the
reporter hook, unhandled request errors are reported (ordinary 4xx are not),
nothing identifying or secret is sent, and the background loops don't die
silently."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_logging  # noqa: E402
import error_monitoring  # noqa: E402
from error_monitoring import (  # noqa: E402
    DISTINCT_ID, ErrorReportingMiddleware, _before_send, init_error_monitoring, scrub,
)

KEY = "phc_testkey"


@pytest.fixture(autouse=True)
def _reset():
    yield
    floorwatch_logging.set_error_reporter(None)
    error_monitoring._client = None


def _fake_posthog():
    module = MagicMock()
    return module, module.Posthog.return_value


def test_monitoring_is_off_without_a_key():
    assert init_error_monitoring("") is False
    assert floorwatch_logging._error_reporter is None


def test_missing_sdk_degrades_quietly():
    with patch.dict(sys.modules, {"posthog": None}):
        assert init_error_monitoring(KEY) is False


def test_sdk_failing_to_start_never_raises():
    module, _client = _fake_posthog()
    module.Posthog.side_effect = ValueError("bad host")
    with patch.dict(sys.modules, {"posthog": module}):
        assert init_error_monitoring(KEY, host="nonsense") is False
    assert error_monitoring._client is None


def test_init_configures_privacy_settings_and_wires_the_reporter():
    module, _client = _fake_posthog()
    with patch.dict(sys.modules, {"posthog": module}):
        assert init_error_monitoring(KEY, host="https://eu.i.posthog.com",
                                     environment="prod", release="abc123") is True
    args, kwargs = module.Posthog.call_args
    assert args == (KEY,)
    assert kwargs["host"] == "https://eu.i.posthog.com"
    assert kwargs["enable_exception_autocapture"] is True
    assert kwargs["capture_exception_code_variables"] is False   # locals can hold secrets
    assert kwargs["disable_geoip"] is True
    assert kwargs["super_properties"] == {"service": "rules-engine", "environment": "prod", "release": "abc123"}
    assert floorwatch_logging._error_reporter is not None


def test_error_level_logs_are_reported_with_the_service_wide_identity():
    module, client = _fake_posthog()
    with patch.dict(sys.modules, {"posthog": module}):
        init_error_monitoring(KEY)
    log = floorwatch_logging.get_logger("rules-engine.test")

    log("just information")
    log("something odd", level="warning")
    assert client.capture_exception.call_count == 0

    log("push delivery failed", level="error")
    assert client.capture_exception.call_count == 1
    exc = client.capture_exception.call_args.args[0]
    kwargs = client.capture_exception.call_args.kwargs
    assert str(exc) == "push delivery failed"
    assert kwargs["distinct_id"] == DISTINCT_ID   # a service, never a person
    assert kwargs["properties"]["logger"] == "rules-engine.test"


def test_a_broken_reporter_never_breaks_logging():
    def boom(*_a):
        raise RuntimeError("monitoring backend down")

    floorwatch_logging.set_error_reporter(boom)
    floorwatch_logging.get_logger("t")("still logs", level="error")  # must not raise


def test_tokens_are_scrubbed_everywhere_in_an_event():
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMDEifQ.abcdefghijklmnop"
    assert jwt_like not in scrub(f"failed for token {jwt_like}")
    assert "secret123" not in scrub("Authorization: Bearer secret123")

    event = {"event": "$exception", "properties": {
        "$exception_list": [{"value": f"bad token {jwt_like}"}], "note": "Bearer abc.def"}}
    out = _before_send(event)
    assert jwt_like not in str(out)
    assert "abc.def" not in str(out)


def _app_with_middleware():
    from fastapi import FastAPI, HTTPException
    app = FastAPI()

    @app.get("/boom/{item_id}")
    async def boom(item_id: str):
        raise RuntimeError("kaboom")

    @app.get("/forbidden")
    async def forbidden():
        raise HTTPException(status_code=403, detail="nope")

    app.add_middleware(ErrorReportingMiddleware)
    return app


def test_an_unhandled_request_error_is_reported_and_still_returns_500():
    from fastapi.testclient import TestClient
    module, client = _fake_posthog()
    with patch.dict(sys.modules, {"posthog": module}):
        init_error_monitoring(KEY)

    http = TestClient(_app_with_middleware(), raise_server_exceptions=False)
    resp = http.get("/boom/secret-employee-104?token=abc")

    assert resp.status_code == 500
    assert client.capture_exception.call_count == 1
    props = client.capture_exception.call_args.kwargs["properties"]
    assert props["http_method"] == "GET"
    assert props["route"] == "/boom/{item_id}"          # the template, not the real id
    assert "secret-employee-104" not in str(client.capture_exception.call_args)
    assert "abc" not in str(props)                       # no query string


def test_ordinary_client_errors_are_not_reported():
    from fastapi.testclient import TestClient
    module, client = _fake_posthog()
    with patch.dict(sys.modules, {"posthog": module}):
        init_error_monitoring(KEY)

    http = TestClient(_app_with_middleware(), raise_server_exceptions=False)
    assert http.get("/forbidden").status_code == 403
    assert http.get("/does-not-exist").status_code == 404
    assert client.capture_exception.call_count == 0


def test_shutdown_flushes_and_is_safe_when_off():
    error_monitoring.shutdown()  # off: must be a no-op
    module, client = _fake_posthog()
    with patch.dict(sys.modules, {"posthog": module}):
        init_error_monitoring(KEY)
    error_monitoring.shutdown()
    client.shutdown.assert_called_once()
    assert error_monitoring._client is None


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
