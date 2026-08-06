"""
MCP server — build brief Phase 5 task 5: "Build an MCP server that wraps
read-only endpoints of the event-store API (current zone status, current
task status, historical query) as callable tools."

Registers exactly three tools, all wrapping `tools.ReadOnlyTools` (the
single shared implementation — see that module's docstring), each
annotated `read_only_hint=True` per the MCP protocol's own tool
annotation spec, so any MCP-aware client (this project's or a third
party's) can see the read-only guarantee declared at the protocol level,
not just infer it from behavior.

No other tool is ever registered on this server — there are exactly
three uses of the `tool` registration decorator anywhere in this module;
verify by inspection rather than trusting this comment.
"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from tools import ReadOnlyTools

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def build_mcp_server(tools: ReadOnlyTools) -> MCPServer:
    server = MCPServer(
        name="floorwatch-intelligence",
        instructions=(
            "Read-only historical and live-status tools for Floorwatch supervisors. "
            "No tool on this server can create, modify, approve, resolve, or delete "
            "anything in zone state, task state, or the notification system."
        ),
    )

    @server.tool(
        name="get_current_zone_status",
        description="Current live coverage status for one zone (or all zones if zone_id is omitted). "
                    "Read-only.",
        annotations=READ_ONLY,
    )
    async def _get_current_zone_status(zone_id: str | None = None) -> dict:
        return await tools.get_current_zone_status(zone_id)

    @server.tool(
        name="get_current_task_status",
        description="Current live effort-tracking status for one task (or all tasks if task_id is "
                    "omitted). Read-only.",
        annotations=READ_ONLY,
    )
    async def _get_current_task_status(task_id: str | None = None) -> dict:
        return await tools.get_current_task_status(task_id)

    @server.tool(
        name="historical_semantic_search",
        description="Semantic search over past shift-digest events and supervisor incident notes. "
                    "Use for questions about patterns, past gaps, or historical context. Read-only.",
        annotations=READ_ONLY,
    )
    def _historical_semantic_search(query: str, zone_id: str | None = None, top_k: int = 5) -> list:
        return tools.historical_semantic_search(query, zone_id=zone_id, top_k=top_k)

    return server
