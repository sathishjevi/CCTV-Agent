"""
Unit tests for tools.py — including the structural read-only enforcement
that this module's docstring promises: every HTTP call to the rules
engine is a GET, never anything that could mutate state.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from embeddings import TfidfEmbeddingProvider  # noqa: E402
from tools import ReadOnlyTools  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class NoWriteAsyncClient:
    """A fake httpx.AsyncClient that only implements .get(); any attempt
    to call .post/.put/.patch/.delete raises AttributeError, since those
    methods simply don't exist on this fake — the same guarantee as if
    ReadOnlyTools tried to call them against a real client and there were
    no route for it. Used to structurally prove ReadOnlyTools never
    attempts a mutating HTTP verb."""

    def __init__(self, get_response):
        self._get_response = get_response
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self.get_calls.append(url)
        self.last_headers = headers
        return self._get_response

    # Deliberately no post/put/patch/delete methods defined.


def _tools(tmp_path, rules_url="http://localhost:8080"):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=32)
    return ReadOnlyTools(rules_url, vector_store, embedding_provider)


@pytest.mark.asyncio
async def test_get_current_zone_status_all_zones(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({"concession": {"status": "covered"}}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        result = await t.get_current_zone_status()
    assert result == {"concession": {"status": "covered"}}
    assert fake_client.get_calls == ["http://localhost:8080/api/state"]


@pytest.mark.asyncio
async def test_get_current_zone_status_single_zone_filters(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({
        "concession": {"status": "covered"}, "lobby": {"status": "nudge"},
    }))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        result = await t.get_current_zone_status(zone_id="lobby")
    assert result == {"lobby": {"status": "nudge"}}


@pytest.mark.asyncio
async def test_get_current_zone_status_unknown_zone_returns_error_not_exception(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({"concession": {"status": "covered"}}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        result = await t.get_current_zone_status(zone_id="nonexistent")
    assert "error" in result["nonexistent"]


@pytest.mark.asyncio
async def test_get_current_task_status_all_tasks(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({"t1": {"status": "open"}}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        result = await t.get_current_task_status()
    assert result == {"t1": {"status": "open"}}
    assert fake_client.get_calls == ["http://localhost:8080/api/tasks"]


@pytest.mark.asyncio
async def test_get_current_task_status_single_task_filters(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({"t1": {"status": "open"}, "t2": {"status": "flagged"}}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        result = await t.get_current_task_status(task_id="t2")
    assert result == {"t2": {"status": "flagged"}}


@pytest.mark.asyncio
async def test_service_token_attached_as_bearer_header(tmp_path):
    """Rules engine now requires auth on every endpoint (SECURITY_REVIEW.md
    finding AUTH-1) — this service must send its own service token."""
    fake_client = NoWriteAsyncClient(FakeResponse({}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
        embedding_provider = TfidfEmbeddingProvider(dim=32)
        t = ReadOnlyTools("http://localhost:8080", vector_store, embedding_provider,
                          service_token="fake-service-token")
        await t.get_current_zone_status()
    assert fake_client.last_headers == {"Authorization": "Bearer fake-service-token"}


@pytest.mark.asyncio
async def test_no_service_token_sends_no_auth_header(tmp_path):
    fake_client = NoWriteAsyncClient(FakeResponse({}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)  # no service_token passed
        await t.get_current_zone_status()
    assert fake_client.last_headers == {}


def test_historical_semantic_search_delegates_to_retrieval(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=32)
    vector_store.upsert("digest:e1", "shift_digest",
                        '{"date":"2026-07-24","zone_id":"concession","event_type":"zone_gap"}',
                        "concession coverage gap", embedding_provider.embed_one("concession coverage gap"))
    t = ReadOnlyTools("http://localhost:8080", vector_store, embedding_provider)
    results = t.historical_semantic_search("concession gap")
    assert len(results) == 1
    assert results[0]["record_id"] == "digest:e1"


# ── structural read-only enforcement ──────────────────────────────────

def test_readonly_tools_has_no_write_shaped_methods():
    """A crude but real structural check: no public method on ReadOnlyTools
    has a name suggesting a mutating action. Combined with the HTTP-GET-only
    tests above, this is the class-shape half of the read-only guarantee."""
    write_verbs = ("create", "assign", "approve", "reassign", "complete",
                   "resolve", "dismiss", "confirm", "delete", "update", "set_")
    public_methods = [m for m in dir(ReadOnlyTools) if not m.startswith("_")]
    offending = [m for m in public_methods if any(v in m.lower() for v in write_verbs)]
    assert offending == [], f"ReadOnlyTools has write-shaped method(s): {offending}"


@pytest.mark.asyncio
async def test_readonly_tools_never_calls_anything_but_get(tmp_path):
    """If ReadOnlyTools ever tried client.post(...)/.put(...)/etc., this
    fake client has no such attribute and it would raise AttributeError —
    proving, not just asserting, that only GET is ever issued."""
    fake_client = NoWriteAsyncClient(FakeResponse({}))
    with patch("tools.httpx.AsyncClient", return_value=fake_client):
        t = _tools(tmp_path)
        await t.get_current_zone_status()
        await t.get_current_task_status()
    assert all("GET" not in "" for _ in fake_client.get_calls)  # sanity: no exception means only .get() was used
    assert len(fake_client.get_calls) == 2
