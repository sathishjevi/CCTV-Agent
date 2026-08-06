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
"""

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


def build_assistant(config, mcp_server) -> Optional[SupervisorAssistant]:
    """Returns None (not a raising error) if no API key is configured —
    main.py surfaces that as a clear 503 from /api/chat rather than a
    crash, same fallback-friendly pattern as this project's other
    optional-external-service integrations."""
    if not config.ANTHROPIC_API_KEY:
        log("No ANTHROPIC_API_KEY configured — chat assistant unavailable.")
        return None
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return SupervisorAssistant(client, mcp_server, config.ANTHROPIC_MODEL, config.MAX_TOOL_ITERATIONS)
