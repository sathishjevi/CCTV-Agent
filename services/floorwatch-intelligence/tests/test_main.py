"""
Tests for main.py — the FastAPI app wiring everything together. Includes
a structural source-scan proving this service's own code never issues a
mutating HTTP call to the rules engine (Phase 5 task 7's "verify this at
the API layer, not just by omitting UI buttons").
"""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

TEST_AUTH_SECRET = "test-fixture-secret-needs-32-bytes-minimum"


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
    monkeypatch.setattr(config, "SQLITE_VECTOR_DB_PATH", tmp_path / "v.sqlite3")
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "INCIDENT_NOTES_PATH", tmp_path / "notes.jsonl")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")  # no real key in this sandbox
    monkeypatch.setattr(config, "AUTH_SECRET", TEST_AUTH_SECRET)

    for mod in ("main", "tools", "mcp_server", "llm", "vector_store", "embeddings", "ingest"):
        sys.modules.pop(mod, None)
    import main as main_module

    from floorwatch_auth import issue_token
    token = issue_token(TEST_AUTH_SECRET, "test-supervisor", "supervisor")

    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client, main_module


def test_healthz(app_client):
    client, main_module = app_client
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["assistant_available"] is False  # no ANTHROPIC_API_KEY in this sandbox


def test_api_tools_lists_exactly_three_read_only_tools(app_client):
    client, _ = app_client
    resp = client.get("/api/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert len(tools) == 3
    assert all(t["read_only"] is True for t in tools)
    assert {t["name"] for t in tools} == {
        "get_current_zone_status", "get_current_task_status", "historical_semantic_search"}


def test_chat_returns_503_without_api_key(app_client):
    client, _ = app_client
    resp = client.post("/api/chat", json={"question": "anything"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["error"]


def test_chat_rate_limit_returns_429_after_limit_exceeded(app_client):
    """SECURITY_REVIEW.md H2 — bounds spend/DoS exposure once a real API
    key is configured. Exercised here via the 503 (no-key) path since that
    still runs the rate-limit check first."""
    client, main_module = app_client
    limit = main_module.config.CHAT_RATE_LIMIT_PER_MINUTE
    for _ in range(limit):
        resp = client.post("/api/chat", json={"question": "anything"})
        assert resp.status_code == 503  # still under the limit, just no API key
    resp = client.post("/api/chat", json={"question": "one too many"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_create_and_list_incident_notes(app_client):
    client, _ = app_client
    resp = client.post("/api/incident-notes", json={"text": "Spill cleaned up", "zone_id": "lobby"})
    assert resp.status_code == 200
    note = resp.json()
    assert note["zone_id"] == "lobby"

    listed = client.get("/api/incident-notes").json()
    assert len(listed) == 1
    assert listed[0]["note_id"] == note["note_id"]


def test_incident_note_author_comes_from_token_not_request_body(app_client):
    """SECURITY_REVIEW.md flagged the old `author` request field as
    spoofable — anyone could attribute a note to anyone. It's gone now;
    the authenticated caller's own username is used unconditionally."""
    client, _ = app_client
    resp = client.post("/api/incident-notes", json={
        "text": "Note", "zone_id": "lobby", "author": "someone-else-entirely",
    })
    assert resp.status_code == 200  # extra "author" field is just ignored by the pydantic model
    assert resp.json()["author"] == "test-supervisor"


def test_endpoints_reject_missing_auth(app_client):
    _client, main_module = app_client
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as anonymous_client:
        resp = anonymous_client.get("/api/tools")
    assert resp.status_code == 401


def test_incident_note_post_requires_supervisor_role(app_client, monkeypatch):
    from floorwatch_auth import issue_token
    import config
    viewer_token = issue_token(config.AUTH_SECRET, "a-viewer", "viewer")
    client, _ = app_client
    resp = client.post("/api/incident-notes",
                       json={"text": "hi", "zone_id": "lobby"},
                       headers={"Authorization": f"Bearer {viewer_token}"})
    assert resp.status_code == 403


def test_incident_note_gets_indexed_and_is_searchable(app_client):
    client, main_module = app_client
    client.post("/api/incident-notes", json={"text": "Concession understaffed most of the shift", "zone_id": "concession"})
    assert len(main_module.vector_store.all_ids()) == 1


def test_ingest_runs_on_startup_and_picks_up_existing_digest(tmp_path, monkeypatch):
    import config
    digest_path = tmp_path / "digest.jsonl"
    digest_path.write_text(
        '{"event_id":"e1","timestamp":"2026-07-24T08:00:00Z","zone_id":"concession","event_type":"zone_gap","confidence":0.9}'
    )
    monkeypatch.setattr(config, "POSTGRES_DSN", "")
    monkeypatch.setattr(config, "SQLITE_VECTOR_DB_PATH", tmp_path / "v.sqlite3")
    monkeypatch.setattr(config, "DIGEST_PATH", digest_path)
    monkeypatch.setattr(config, "INCIDENT_NOTES_PATH", tmp_path / "notes.jsonl")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")

    for mod in ("main", "tools", "mcp_server", "llm", "vector_store", "embeddings", "ingest"):
        sys.modules.pop(mod, None)
    import main as main_module

    from fastapi.testclient import TestClient
    with TestClient(main_module.app):
        pass
    assert len(main_module.vector_store.all_ids()) == 1


# ── structural "no write path" scan (Phase 5 task 7) ─────────────────────

# Actual Python call syntax for an OUTBOUND mutating HTTP client call —
# deliberately not "app.post("/"@app.post(" (this service's own inbound
# FastAPI route registrations for /api/chat and /api/incident-notes,
# which are this phase's legitimate scope) and deliberately not a
# plain-English word list (that false-positives on this project's own
# docstrings/system-prompt text explaining what ISN'T allowed, e.g. "You
# cannot approve a directive").
OUTBOUND_MUTATING_CALL_PATTERNS = ["client.post(", "client.put(", "client.patch(", "client.delete(",
                                   "httpx.post(", "httpx.put(", "httpx.patch(", "httpx.delete("]


def test_no_app_module_ever_issues_an_outbound_mutating_http_call():
    """None of this service's own modules may contain the Python call
    syntax for an outbound mutating HTTP client call anywhere — the only
    outbound calls to floorwatch-rules-engine this service makes at all
    are the two GETs inside tools.py, and this service has no other
    outbound HTTP target. (This service's own @app.post(...) route
    registrations for /api/chat and /api/incident-notes are inbound and
    out of scope for this check — see incident_notes.py's docstring for
    why those writes are this phase's legitimate scope.)"""
    service_dir = Path(__file__).resolve().parent.parent
    offending = []
    for py_file in (service_dir / "app").glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for pattern in OUTBOUND_MUTATING_CALL_PATTERNS:
            if pattern in text:
                offending.append((py_file.name, pattern))
    assert offending == [], f"Found outbound mutating HTTP call syntax: {offending}"


def test_tools_module_only_issues_get_requests():
    tools_source = (Path(__file__).resolve().parent.parent / "app" / "tools.py").read_text()
    assert "client.get(" in tools_source
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in tools_source, f"tools.py must never call client{verb}"
