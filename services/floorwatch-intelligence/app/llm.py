"""
LLM integration — build brief Phase 5 tasks 4 + 6:

  "Integrate an LLM (e.g. Claude via API) to answer supervisor questions,
  grounding answers in retrieved records — cite which shift/date/zone the
  answer is drawn from rather than answering from general knowledge."

  "Wire the MCP tools into the LLM's available tool set; test multi-step
  queries that require both retrieval (history) and a live tool call
  (current status) in the same answer."

Tool definitions and execution are derived directly from the MCP server
(mcp_server.py) via `list_tools()`/`call_tool()` — not a second,
hand-rolled tool set — so this chat layer can never call anything the MCP
server doesn't expose. Combined with mcp_server.py registering only three
read-only tools, this is the structural half of Phase 5 task 7's "no
write path... verify this at the API layer" (the system-prompt wording
below is the other, weaker half — a prompt is persuasion, not a
guarantee; the tool registry is the guarantee).

No `ANTHROPIC_API_KEY` is available in this dev sandbox for this
application to call out with (distinct from whatever access the coding
agent that built this has to Claude). The integration below is real,
production code — `tests/test_llm.py` exercises it against a mocked
Anthropic client, not the live API. Flagged in PHASE_5_NOTES.md.

── Multi-provider support ──────────────────────────────────────────────
Three assistant classes below all expose the same public contract —
`async def answer(self, question: str) -> {"answer": str, "tool_calls":
[...]}` — so main.py and the guardrails/system-prompt logic never need to
know which vendor is behind `assistant`:

  - SupervisorAssistant          — Anthropic's Messages API (Claude)
  - OpenAICompatibleAssistant    — OpenAI's chat-completions + function-
    calling wire format. This covers real ChatGPT *and* anything hosted
    behind an OpenAI-compatible endpoint (LLM_BASE_URL) — Kimi/Moonshot,
    DeepSeek, and Llama served via Groq/Together/Fireworks/Ollama/vLLM
    all speak this format, which is why one adapter covers all of them
    rather than needing a class per vendor.
  - GeminiAssistant               — Google Gemini's own function-calling
    API shape, which is not OpenAI-compatible and needs its own adapter.

Each vendor SDK is imported lazily inside its class's constructor (same
pattern as the cloud-storage SDKs in skills/detection/floorwatch-ingest)
so a deployment using only one provider never needs the other two SDKs
installed.
"""

import json
import sys
from typing import Optional

SYSTEM_PROMPT = """You are Floorwatch's read-only supervisor intelligence assistant.

Rules you must follow, without exception:
1. Only answer using information returned by your tools. Never answer from general knowledge about the world, and never guess at zone names, dates, or statuses you have not retrieved.
2. Always cite your sources: when you reference a past event, quote the citation tag you were given (e.g. "[shift digest, 2026-07-24, zone=concession, zone_gap]"). When you reference live status, say so explicitly (e.g. "as of the current live status check").
3. You have NO ability to change anything in the Floorwatch system. You cannot send a nudge, approve or draft a supervisor directive, resolve a zone, assign or complete a task, or trigger any notification. If asked to do any of these things, or asked to pretend you did, refuse clearly and explain you are read-only — do not simulate having done it, do not describe what it would look like as if it happened.
4. If your tools don't return enough information to answer confidently, say so rather than filling the gap with a guess.
"""


def log(msg: str):
    print(f"[llm] {msg}", file=sys.stderr, flush=True)


def _mcp_tools_to_anthropic_schema(mcp_tools) -> list:
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in mcp_tools
    ]


class SupervisorAssistant:
    def __init__(self, anthropic_client, mcp_server, model: str, max_tool_iterations: int = 5):
        self._client = anthropic_client
        self._mcp_server = mcp_server
        self.model = model
        self.max_tool_iterations = max_tool_iterations

    async def answer(self, question: str) -> dict:
        """Runs the tool-use loop and returns {"answer": str, "tool_calls": [...]}.
        tool_calls is returned for transparency/testability — every entry
        is a call that actually went through mcp_server.call_tool(), i.e.
        one of exactly three read-only tools."""
        mcp_tools = await self._mcp_server.list_tools()
        tool_schemas = _mcp_tools_to_anthropic_schema(mcp_tools)

        messages = [{"role": "user", "content": question}]
        tool_calls_made = []

        for _ in range(self.max_tool_iterations):
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=tool_schemas,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                text = "".join(b.text for b in response.content if b.type == "text")
                return {"answer": text, "tool_calls": tool_calls_made}

            tool_results = []
            for block in tool_use_blocks:
                result = await self._mcp_server.call_tool(block.name, block.input)
                tool_calls_made.append({"name": block.name, "input": block.input})
                result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": result.is_error,
                })
            messages.append({"role": "user", "content": tool_results})

        log(f"Hit max_tool_iterations ({self.max_tool_iterations}) without a final answer for: {question!r}")
        return {
            "answer": "I wasn't able to finish answering within the allotted tool-use steps — "
                      "try narrowing the question.",
            "tool_calls": tool_calls_made,
        }


def _mcp_tools_to_openai_schema(mcp_tools) -> list:
    return [
        {"type": "function", "function": {
            "name": t.name, "description": t.description or "", "parameters": t.input_schema,
        }}
        for t in mcp_tools
    ]


def _mcp_tools_to_gemini_schema(mcp_tools):
    from google.genai import types
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t.name, description=t.description or "", parameters=t.input_schema)
        for t in mcp_tools
    ])]


class OpenAICompatibleAssistant:
    """Speaks the OpenAI chat-completions + function-calling wire format —
    used for real ChatGPT, and for any other host that implements the
    same format behind a custom base_url (Kimi/Moonshot, DeepSeek,
    Groq/Together/Fireworks/Ollama-hosted Llama, etc). See llm.py's
    module docstring for why one adapter covers all of these."""

    def __init__(self, client, mcp_server, model: str, max_tool_iterations: int = 5):
        self._client = client
        self._mcp_server = mcp_server
        self.model = model
        self.max_tool_iterations = max_tool_iterations

    async def answer(self, question: str) -> dict:
        mcp_tools = await self._mcp_server.list_tools()
        tool_schemas = _mcp_tools_to_openai_schema(mcp_tools)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        tool_calls_made = []

        for _ in range(self.max_tool_iterations):
            response = await self._client.chat.completions.create(
                model=self.model, max_tokens=1024, messages=messages, tools=tool_schemas,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return {"answer": msg.content or "", "tool_calls": tool_calls_made}

            messages.append({
                "role": "assistant", "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                tool_input = json.loads(tc.function.arguments or "{}")
                result = await self._mcp_server.call_tool(tc.function.name, tool_input)
                tool_calls_made.append({"name": tc.function.name, "input": tool_input})
                result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

        log(f"Hit max_tool_iterations ({self.max_tool_iterations}) without a final answer for: {question!r}")
        return {
            "answer": "I wasn't able to finish answering within the allotted tool-use steps — "
                      "try narrowing the question.",
            "tool_calls": tool_calls_made,
        }


class GeminiAssistant:
    """Google Gemini's function-calling API — not OpenAI-compatible, so
    it gets its own adapter rather than reusing OpenAICompatibleAssistant."""

    def __init__(self, client, mcp_server, model: str, max_tool_iterations: int = 5):
        self._client = client
        self._mcp_server = mcp_server
        self.model = model
        self.max_tool_iterations = max_tool_iterations

    async def answer(self, question: str) -> dict:
        from google.genai import types

        mcp_tools = await self._mcp_server.list_tools()
        tools = _mcp_tools_to_gemini_schema(mcp_tools)
        gen_config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=tools)

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]
        tool_calls_made = []

        for _ in range(self.max_tool_iterations):
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=gen_config,
            )
            candidate = response.candidates[0]
            parts = candidate.content.parts or []
            function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            if not function_calls:
                text = "".join(p.text for p in parts if getattr(p, "text", None))
                return {"answer": text, "tool_calls": tool_calls_made}

            contents.append(candidate.content)
            response_parts = []
            for fc in function_calls:
                tool_input = dict(fc.args or {})
                result = await self._mcp_server.call_tool(fc.name, tool_input)
                tool_calls_made.append({"name": fc.name, "input": tool_input})
                result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                response_parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result_text}))
            contents.append(types.Content(role="user", parts=response_parts))

        log(f"Hit max_tool_iterations ({self.max_tool_iterations}) without a final answer for: {question!r}")
        return {
            "answer": "I wasn't able to finish answering within the allotted tool-use steps — "
                      "try narrowing the question.",
            "tool_calls": tool_calls_made,
        }


def build_assistant(config, mcp_server):
    """Returns None (not a raising error) if no API key is configured —
    main.py surfaces that as a clear 503 from /api/chat rather than a
    crash, same fallback-friendly pattern as this project's other
    optional-external-service integrations.

    Dispatches on config.LLM_PROVIDER ("anthropic" | "openai" | "gemini",
    default "anthropic") to one of the three assistant classes above —
    see llm.py's module docstring for why "openai" covers more than just
    ChatGPT."""
    provider = (getattr(config, "LLM_PROVIDER", "") or "anthropic").strip().lower()
    max_iter = getattr(config, "MAX_TOOL_ITERATIONS", 5)

    if provider == "anthropic":
        api_key = getattr(config, "ANTHROPIC_API_KEY", "") or getattr(config, "LLM_API_KEY", "")
        if not api_key:
            log("No ANTHROPIC_API_KEY (or FLOORWATCH_LLM_API_KEY) configured — chat assistant unavailable.")
            return None
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        model = getattr(config, "ANTHROPIC_MODEL", "") or getattr(config, "LLM_MODEL", "") or "claude-sonnet-4-5"
        return SupervisorAssistant(client, mcp_server, model, max_iter)

    api_key = getattr(config, "LLM_API_KEY", "")
    model = getattr(config, "LLM_MODEL", "")
    if not api_key:
        log(f"No FLOORWATCH_LLM_API_KEY configured for provider '{provider}' — chat assistant unavailable.")
        return None
    if not model:
        log(f"No FLOORWATCH_LLM_MODEL configured for provider '{provider}' — chat assistant unavailable "
            f"(there's no safe default model name across vendors, so this must be set explicitly).")
        return None

    if provider == "openai":
        import openai
        base_url = getattr(config, "LLM_BASE_URL", "") or None
        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        return OpenAICompatibleAssistant(client, mcp_server, model, max_iter)

    if provider == "gemini":
        from google import genai
        client = genai.Client(api_key=api_key)
        return GeminiAssistant(client, mcp_server, model, max_iter)

    log(f"Unknown FLOORWATCH_LLM_PROVIDER '{provider}' — expected 'anthropic', 'openai', or 'gemini'. "
        f"Chat assistant unavailable.")
    return None
