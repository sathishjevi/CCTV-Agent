"""
Tests for mcp_server.py — proves the MCP server exposes exactly the three
read-only tools promised, each declared read-only at the protocol level
(`read_only_hint=True`), and that calling them actually works end-to-end
through the real MCP server object (not a reimplementation of it).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from embeddings import TfidfEmbeddingProvider  # noqa: E402
from mcp_server import build_mcp_server  # noqa: E402
from tools import ReadOnlyTools  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402


def _server(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=32)
    tools = ReadOnlyTools("http://localhost:8080", vector_store, embedding_provider)
    return build_mcp_server(tools), vector_store, embedding_provider


@pytest.mark.asyncio
async def test_server_exposes_exactly_three_tools(tmp_path):
    server, _, _ = _server(tmp_path)
    tool_list = await server.list_tools()
    names = {t.name for t in tool_list}
    assert names == {"get_current_zone_status", "get_current_task_status", "historical_semantic_search"}


@pytest.mark.asyncio
async def test_every_tool_is_declared_read_only(tmp_path):
    server, _, _ = _server(tmp_path)
    tool_list = await server.list_tools()
    for t in tool_list:
        assert t.annotations is not None, f"{t.name} has no annotations"
        assert t.annotations.read_only_hint is True, f"{t.name} is not declared read_only_hint=True"
        assert t.annotations.destructive_hint is False, f"{t.name} is not declared destructive_hint=False"


@pytest.mark.asyncio
async def test_historical_semantic_search_tool_call_returns_results(tmp_path):
    server, vector_store, embedding_provider = _server(tmp_path)
    vector_store.upsert("digest:e1", "shift_digest",
                        json.dumps({"date": "2026-07-24", "zone_id": "concession", "event_type": "zone_gap"}),
                        "concession counter coverage gap unresolved",
                        embedding_provider.embed_one("concession counter coverage gap unresolved"))

    result = await server.call_tool("historical_semantic_search", {"query": "concession gap"})
    assert result.is_error is False
    # The MCP SDK unpacks a list return value into one content block per
    # item, rather than a single JSON-array blob.
    payload = [json.loads(c.text) for c in result.content]
    assert len(payload) == 1
    assert payload[0]["record_id"] == "digest:e1"


@pytest.mark.asyncio
async def test_get_current_zone_status_tool_call_hits_rules_engine(tmp_path):
    server, _, _ = _server(tmp_path)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"concession": {"status": "covered"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/api/state")
            return FakeResponse()

    with patch("tools.httpx.AsyncClient", return_value=FakeClient()):
        result = await server.call_tool("get_current_zone_status", {})
    payload = json.loads(result.content[0].text)
    assert payload == {"concession": {"status": "covered"}}


@pytest.mark.asyncio
async def test_calling_unregistered_tool_fails(tmp_path):
    server, _, _ = _server(tmp_path)
    with pytest.raises(Exception):
        await server.call_tool("approve_zone_command", {"zone_id": "concession"})


def test_mcp_server_module_registers_no_other_tools():
    """Structural check on the source itself: exactly three `@server.tool(`
    decorator usages in mcp_server.py — grep-verifiable, not just tested
    behaviorally above."""
    source = (Path(__file__).resolve().parent.parent / "app" / "mcp_server.py").read_text()
    assert source.count("@server.tool(") == 3
