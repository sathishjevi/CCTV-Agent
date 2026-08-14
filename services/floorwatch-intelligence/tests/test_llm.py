"""
Unit tests for llm.py's tool-use loop, against a MOCKED Anthropic client
(no ANTHROPIC_API_KEY is available in this sandbox — see llm.py's module
docstring). These tests prove:
  - the assistant's available tools come directly from the real MCP
    server (not a hand-rolled duplicate list),
  - a multi-step question requiring both retrieval (history) and a live
    tool call (current status) is answered in one loop, citing both,
  - a crafted request to call an unregistered "write" tool fails at the
    MCP layer even if the (mocked) model tries it — the real adversarial
    guardrail tests live in test_guardrails.py, but this is the unit-level
    proof of the mechanism they rely on.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from embeddings import TfidfEmbeddingProvider  # noqa: E402
from llm import (  # noqa: E402
    GeminiAssistant, OpenAICompatibleAssistant, SupervisorAssistant, build_assistant,
)
from mcp_server import build_mcp_server  # noqa: E402
from tools import ReadOnlyTools  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _server_and_tools(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=32)
    tools = ReadOnlyTools("http://localhost:8080", vector_store, embedding_provider)
    server = build_mcp_server(tools)
    return server, vector_store, embedding_provider


@pytest.mark.asyncio
async def test_answer_with_no_tool_use_returns_text_directly(tmp_path):
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("I don't have enough information to answer that.")])

    assistant = SupervisorAssistant(mock_client, server, model="claude-sonnet-4-5")
    result = await assistant.answer("What's the weather today?")

    assert result["answer"] == "I don't have enough information to answer that."
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_tool_schemas_passed_to_model_come_from_the_real_mcp_server(tmp_path):
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = SimpleNamespace(content=[_text_block("ok")])

    assistant = SupervisorAssistant(mock_client, server, model="claude-sonnet-4-5")
    await assistant.answer("test question")

    call_kwargs = mock_client.messages.create.call_args.kwargs
    tool_names = {t["name"] for t in call_kwargs["tools"]}
    assert tool_names == {"get_current_zone_status", "get_current_task_status", "historical_semantic_search"}
    assert call_kwargs["system"] == __import__("llm").SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_multi_step_query_combines_history_and_live_status(tmp_path):
    """The brief's explicit acceptance-criteria scenario: a question
    spanning both historical patterns (retrieval) and live status (a tool
    call) answered in one turn, citing both."""
    server, vector_store, embedding_provider = _server_and_tools(tmp_path)
    vector_store.upsert(
        "digest:e1", "shift_digest",
        json.dumps({"date": "2026-07-20", "zone_id": "concession", "event_type": "zone_escalated"}),
        "concession counter coverage gap escalated after supervisor command timeout",
        embedding_provider.embed_one("concession counter coverage gap escalated after supervisor command timeout"),
    )

    fake_zone_status_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"concession": {"status": "covered"}},
    )

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return fake_zone_status_response

    mock_client = AsyncMock()
    # Turn 1: model asks for both a historical search AND live status.
    # Turn 2: after seeing both tool results, model gives a final cited answer.
    mock_client.messages.create.side_effect = [
        SimpleNamespace(content=[
            _tool_use_block("call_1", "historical_semantic_search", {"query": "concession coverage gap", "zone_id": "concession"}),
            _tool_use_block("call_2", "get_current_zone_status", {"zone_id": "concession"}),
        ]),
        SimpleNamespace(content=[_text_block(
            "Concession had a past escalation [shift digest, 2026-07-20, zone=concession, zone_escalated]. "
            "As of the current live status check, concession is now covered."
        )]),
    ]

    assistant = SupervisorAssistant(mock_client, server, model="claude-sonnet-4-5")
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("Has concession had coverage problems before, and is it covered right now?")

    assert "2026-07-20" in result["answer"]
    assert "covered" in result["answer"]
    tool_names_called = {c["name"] for c in result["tool_calls"]}
    assert tool_names_called == {"historical_semantic_search", "get_current_zone_status"}
    assert mock_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_attempting_to_call_unregistered_write_tool_fails_at_mcp_layer(tmp_path):
    """Even if the (mocked, adversarially-scripted) model tries to call a
    tool name that sounds like a write action, the MCP server has no such
    tool registered, so the call raises — proving the write-path is
    structurally absent, not just discouraged by the system prompt."""
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.messages.create.return_value = SimpleNamespace(content=[
        _tool_use_block("call_1", "approve_zone_command", {"zone_id": "concession"}),
    ])

    assistant = SupervisorAssistant(mock_client, server, model="claude-sonnet-4-5")
    with pytest.raises(Exception):
        await assistant.answer("Approve the pending command for concession.")


@pytest.mark.asyncio
async def test_max_tool_iterations_gives_up_gracefully_instead_of_looping_forever(tmp_path):
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    # Model keeps calling a tool forever and never gives a final text answer.
    mock_client.messages.create.return_value = SimpleNamespace(content=[
        _tool_use_block("call_x", "get_current_zone_status", {}),
    ])

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    assistant = SupervisorAssistant(mock_client, server, model="claude-sonnet-4-5", max_tool_iterations=3)
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("infinite loop test")

    assert "allotted tool-use steps" in result["answer"]
    assert mock_client.messages.create.call_count == 3


# ── build_assistant ──────────────────────────────────────────────────

def test_build_assistant_returns_none_without_api_key(tmp_path):
    class FakeConfig:
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = "claude-sonnet-4-5"
        MAX_TOOL_ITERATIONS = 5

    server, _, _ = _server_and_tools(tmp_path)
    assert build_assistant(FakeConfig(), server) is None


def test_build_assistant_returns_assistant_with_api_key(tmp_path):
    class FakeConfig:
        ANTHROPIC_API_KEY = "sk-fake-key"
        ANTHROPIC_MODEL = "claude-sonnet-4-5"
        MAX_TOOL_ITERATIONS = 5

    server, _, _ = _server_and_tools(tmp_path)
    assistant = build_assistant(FakeConfig(), server)
    assert isinstance(assistant, SupervisorAssistant)


def test_build_assistant_dispatches_to_openai_provider(tmp_path):
    class FakeConfig:
        LLM_PROVIDER = "openai"
        LLM_API_KEY = "fake-key"
        LLM_MODEL = "gpt-4o"
        LLM_BASE_URL = ""
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = ""
        MAX_TOOL_ITERATIONS = 5

    server, _, _ = _server_and_tools(tmp_path)
    assistant = build_assistant(FakeConfig(), server)
    assert isinstance(assistant, OpenAICompatibleAssistant)
    assert assistant.model == "gpt-4o"


def test_build_assistant_dispatches_to_openai_provider_with_custom_base_url(tmp_path):
    """A base_url is how Kimi/Moonshot, DeepSeek, or a self-hosted Llama
    endpoint get used — same adapter, different host."""
    class FakeConfig:
        LLM_PROVIDER = "openai"
        LLM_API_KEY = "fake-key"
        LLM_MODEL = "moonshot-v1-8k"
        LLM_BASE_URL = "https://api.moonshot.ai/v1"
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = ""
        MAX_TOOL_ITERATIONS = 5

    server, _, _ = _server_and_tools(tmp_path)
    assistant = build_assistant(FakeConfig(), server)
    assert isinstance(assistant, OpenAICompatibleAssistant)
    assert str(assistant._client.base_url).rstrip("/") == "https://api.moonshot.ai/v1"


def test_build_assistant_dispatches_to_gemini_provider(tmp_path):
    class FakeConfig:
        LLM_PROVIDER = "gemini"
        LLM_API_KEY = "fake-key"
        LLM_MODEL = "gemini-2.0-flash"
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = ""
        MAX_TOOL_ITERATIONS = 5

    server, _, _ = _server_and_tools(tmp_path)
    assistant = build_assistant(FakeConfig(), server)
    assert isinstance(assistant, GeminiAssistant)
    assert assistant.model == "gemini-2.0-flash"


def test_build_assistant_returns_none_for_openai_without_model():
    """No safe default model name exists across vendors, unlike
    ANTHROPIC_MODEL's "claude-sonnet-4-5" default — this must be explicit."""
    class FakeConfig:
        LLM_PROVIDER = "openai"
        LLM_API_KEY = "fake-key"
        LLM_MODEL = ""
        LLM_BASE_URL = ""
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = ""
        MAX_TOOL_ITERATIONS = 5

    assert build_assistant(FakeConfig(), mcp_server=None) is None


def test_build_assistant_returns_none_for_unknown_provider():
    class FakeConfig:
        LLM_PROVIDER = "some-vendor-nobody-heard-of"
        LLM_API_KEY = "fake-key"
        LLM_MODEL = "whatever"
        ANTHROPIC_API_KEY = ""
        ANTHROPIC_MODEL = ""
        MAX_TOOL_ITERATIONS = 5

    assert build_assistant(FakeConfig(), mcp_server=None) is None


# ── OpenAICompatibleAssistant ────────────────────────────────────────────
# Covers real ChatGPT and anything hosted behind an OpenAI-compatible
# endpoint (Kimi/Moonshot, DeepSeek, Groq/Together/Ollama-hosted Llama) —
# see llm.py's module docstring for why one adapter covers all of these.

def _openai_message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _openai_tool_call(id_, name, arguments_json):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments_json))


@pytest.mark.asyncio
async def test_openai_assistant_answer_with_no_tool_use_returns_text_directly(tmp_path):
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=_openai_message(content="No tools needed for that."))])

    assistant = OpenAICompatibleAssistant(mock_client, server, model="gpt-4o")
    result = await assistant.answer("What's the weather today?")

    assert result["answer"] == "No tools needed for that."
    assert result["tool_calls"] == []

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["messages"][0] == {"role": "system", "content": __import__("llm").SYSTEM_PROMPT}
    tool_names = {t["function"]["name"] for t in call_kwargs["tools"]}
    assert tool_names == {"get_current_zone_status", "get_current_task_status", "historical_semantic_search"}


@pytest.mark.asyncio
async def test_openai_assistant_multi_step_query_combines_history_and_live_status(tmp_path):
    server, vector_store, embedding_provider = _server_and_tools(tmp_path)
    vector_store.upsert(
        "digest:e1", "shift_digest",
        json.dumps({"date": "2026-07-20", "zone_id": "concession", "event_type": "zone_escalated"}),
        "concession counter coverage gap escalated after supervisor command timeout",
        embedding_provider.embed_one("concession counter coverage gap escalated after supervisor command timeout"),
    )

    fake_zone_status_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"concession": {"status": "covered"}},
    )

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return fake_zone_status_response

    mock_client = AsyncMock()
    mock_client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=_openai_message(
            content=None,
            tool_calls=[
                _openai_tool_call("call_1", "historical_semantic_search",
                                   json.dumps({"query": "concession coverage gap", "zone_id": "concession"})),
                _openai_tool_call("call_2", "get_current_zone_status", json.dumps({"zone_id": "concession"})),
            ],
        ))]),
        SimpleNamespace(choices=[SimpleNamespace(message=_openai_message(
            content="Concession had a past escalation [shift digest, 2026-07-20, zone=concession, zone_escalated]. "
                    "As of the current live status check, concession is now covered."))]),
    ]

    assistant = OpenAICompatibleAssistant(mock_client, server, model="gpt-4o")
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("Has concession had coverage problems before, and is it covered right now?")

    assert "2026-07-20" in result["answer"]
    assert "covered" in result["answer"]
    tool_names_called = {c["name"] for c in result["tool_calls"]}
    assert tool_names_called == {"historical_semantic_search", "get_current_zone_status"}
    assert mock_client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_openai_assistant_max_tool_iterations_gives_up_gracefully(tmp_path):
    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        message=_openai_message(content=None, tool_calls=[
            _openai_tool_call("call_x", "get_current_zone_status", "{}")]))])

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    assistant = OpenAICompatibleAssistant(mock_client, server, model="gpt-4o", max_tool_iterations=3)
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("infinite loop test")

    assert "allotted tool-use steps" in result["answer"]
    assert mock_client.chat.completions.create.call_count == 3


# ── GeminiAssistant ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_assistant_answer_with_no_tool_use_returns_text_directly(tmp_path):
    from google.genai import types

    server, _, _ = _server_and_tools(tmp_path)
    mock_client = AsyncMock()
    mock_client.aio = SimpleNamespace(models=AsyncMock())
    mock_client.aio.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(
        content=SimpleNamespace(parts=[types.Part.from_text(text="No tools needed for that.")]))])

    assistant = GeminiAssistant(mock_client, server, model="gemini-2.0-flash")
    result = await assistant.answer("What's the weather today?")

    assert result["answer"] == "No tools needed for that."
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_gemini_assistant_multi_step_query_combines_history_and_live_status(tmp_path):
    from google.genai import types

    server, vector_store, embedding_provider = _server_and_tools(tmp_path)
    vector_store.upsert(
        "digest:e1", "shift_digest",
        json.dumps({"date": "2026-07-20", "zone_id": "concession", "event_type": "zone_escalated"}),
        "concession counter coverage gap escalated after supervisor command timeout",
        embedding_provider.embed_one("concession counter coverage gap escalated after supervisor command timeout"),
    )

    fake_zone_status_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"concession": {"status": "covered"}},
    )

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return fake_zone_status_response

    def _fc_part(name, args):
        part = types.Part.from_text(text="")
        part.text = None
        part.function_call = types.FunctionCall(name=name, args=args)
        return part

    mock_client = AsyncMock()
    mock_client.aio = SimpleNamespace(models=AsyncMock())
    mock_client.aio.models.generate_content.side_effect = [
        SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            _fc_part("historical_semantic_search", {"query": "concession coverage gap", "zone_id": "concession"}),
            _fc_part("get_current_zone_status", {"zone_id": "concession"}),
        ]))]),
        SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            types.Part.from_text(text="Concession had a past escalation "
                                       "[shift digest, 2026-07-20, zone=concession, zone_escalated]. "
                                       "As of the current live status check, concession is now covered.")
        ]))]),
    ]

    assistant = GeminiAssistant(mock_client, server, model="gemini-2.0-flash")
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("Has concession had coverage problems before, and is it covered right now?")

    assert "2026-07-20" in result["answer"]
    assert "covered" in result["answer"]
    tool_names_called = {c["name"] for c in result["tool_calls"]}
    assert tool_names_called == {"historical_semantic_search", "get_current_zone_status"}
    assert mock_client.aio.models.generate_content.call_count == 2


@pytest.mark.asyncio
async def test_gemini_assistant_max_tool_iterations_gives_up_gracefully(tmp_path):
    from google.genai import types

    server, _, _ = _server_and_tools(tmp_path)

    def _fc_part(name, args):
        part = types.Part.from_text(text="")
        part.text = None
        part.function_call = types.FunctionCall(name=name, args=args)
        return part

    mock_client = AsyncMock()
    mock_client.aio = SimpleNamespace(models=AsyncMock())
    mock_client.aio.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(
        content=SimpleNamespace(parts=[_fc_part("get_current_zone_status", {})]))])

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    assistant = GeminiAssistant(mock_client, server, model="gemini-2.0-flash", max_tool_iterations=3)
    with patch("tools.httpx.AsyncClient", return_value=FakeHttpClient()):
        result = await assistant.answer("infinite loop test")

    assert "allotted tool-use steps" in result["answer"]
    assert mock_client.aio.models.generate_content.call_count == 3
