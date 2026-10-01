"""MCP tool surface over the Oura API, one module per area.

Read-only by design. Nothing here writes to Oura.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools import (
    activity,
    battery,
    heart,
    profile,
    query,
    readiness,
    sleep,
    stress,
    summary,
)
from app.mcp.tools.common import ToolRunner
from app.utilities.provider import ContainerProvider

_MODULES = (summary, sleep, readiness, stress, activity, heart, battery, profile, query)


def register_tools(server: MCPServer, container: ContainerProvider) -> None:
    """Attach every Oura tool to the MCP server."""
    runner = ToolRunner(container)
    for module in _MODULES:
        module.register(server, runner)
