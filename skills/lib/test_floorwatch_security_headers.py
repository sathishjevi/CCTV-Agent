"""Unit test for the shared security-headers middleware (DP-M2,
DATA_PROTECTION_SECURITY_ANALYSIS.md) — built against a minimal throwaway
FastAPI app rather than either real service, so this doesn't need Redis/
Postgres/auth fixtures just to prove the middleware itself works."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("fastapi")

from floorwatch_security_headers import install_security_headers  # noqa: E402


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    install_security_headers(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def test_sets_all_expected_headers(client):
    resp = client.get("/ping")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers["Permissions-Policy"]
    assert "max-age=" in resp.headers["Strict-Transport-Security"]


def test_csp_restricts_default_and_frame_ancestors(client):
    csp = client.get("/ping").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_headers_present_even_on_error_responses(client):
    """A missing route (404) still needs these headers — an attacker
    probing for endpoints shouldn't get an unprotected response."""
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["X-Frame-Options"] == "DENY"
