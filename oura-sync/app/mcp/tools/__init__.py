"""MCP tool surface over the Oura API, one module per area.

Read-only by design. Nothing here writes to Oura.
"""

from __future__ import annotations

from depi import ServiceProvider
from mcp.server.mcpserver import MCPServer

from app.mcp.tools import (
    activity,
    battery,
    cache,
    heart,
    history,
    profile,
    query,
    readiness,
    sleep,
    stress,
    summary,
    wear,
)
from app.mcp.tools.common import ToolRunner

_MODULES = (summary, sleep, readiness, stress, activity, heart, battery, profile, query, history, wear, cache)


def register_tools(server: MCPServer, provider: ServiceProvider) -> None:
    """Attach every Oura tool to the MCP server."""
    runner = ToolRunner(provider)
    for module in _MODULES:
        module.register(server, runner)
