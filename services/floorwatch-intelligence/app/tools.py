"""
Read-only tool implementations — build brief Phase 5 tasks 5/6: "Build an
MCP server that wraps read-only endpoints of the event-store API (current
zone status, current task status, historical query)... Wire the MCP
tools into the LLM's available tool set."

This is the ONE implementation shared identically by the MCP server
(mcp_server.py, for external MCP clients) and the LLM's tool-use loop
(llm.py, via the MCP server's own `list_tools`/`call_tool` — see that
module) — there is no second code path that could drift and accidentally
expose a write action one integration point lacks.

Global Constraint 7 / Phase 5's "read-only" mandate is enforced
structurally here, not just by naming convention: every method below
only ever issues an HTTP GET to the rules engine (never POST/PUT/DELETE/
PATCH), and nothing in this class can create a task, approve a directive,
resolve a zone, or send a notification. `tests/test_readonly_enforcement.py`
asserts this by patching httpx so any non-GET call fails the test, and by
inspecting the class for any method whose name suggests a write action.
"""

from typing import Optional

import httpx


class ReadOnlyTools:
    def __init__(self, rules_engine_base_url: str, vector_store, embedding_provider,
                 service_token: Optional[str] = None):
        self.rules_engine_base_url = rules_engine_base_url.rstrip("/")
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        # Rules engine now requires auth on every endpoint (SECURITY_REVIEW.md
        # finding AUTH-1) — this service authenticates as its own "viewer"-
        # scoped service account, never a supervisor token. See config.py.
        self._headers = {"Authorization": f"Bearer {service_token}"} if service_token else {}

    async def get_current_zone_status(self, zone_id: Optional[str] = None) -> dict:
        """Live Part B coverage status for one zone, or every zone if
        zone_id is omitted. Read-only: GET /api/state on the rules engine."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.rules_engine_base_url}/api/state", headers=self._headers)
            resp.raise_for_status()
            state = resp.json()
        if zone_id:
            return {zone_id: state.get(zone_id, {"error": f"no data for zone '{zone_id}'"})}
        return state

    async def get_current_task_status(self, task_id: Optional[str] = None) -> dict:
        """Live Part A effort-tracking status for one task, or every task
        if task_id is omitted. Read-only: GET /api/tasks on the rules engine."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.rules_engine_base_url}/api/tasks", headers=self._headers)
            resp.raise_for_status()
            tasks = resp.json()
        if task_id:
            return {task_id: tasks.get(task_id, {"error": f"no data for task '{task_id}'"})}
        return tasks

    def historical_semantic_search(self, query: str, zone_id: Optional[str] = None,
                                    top_k: int = 5) -> list:
        """Semantic search over embedded past shift digests and supervisor
        incident notes — purely local index reads, no network call."""
        from retrieval import semantic_search
        return semantic_search(self.vector_store, self.embedding_provider, query,
                               top_k=top_k, zone_id=zone_id)
