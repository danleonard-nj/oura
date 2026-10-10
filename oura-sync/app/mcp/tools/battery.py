"""The ring itself: configuration and firmware."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_ring_info",
        title="Oura ring configuration",
        description=(
            "The user's ring hardware: model, design, colour, size, firmware version and setup "
            "date. Use when a question depends on ring generation, since some metrics are not "
            "available on older hardware. Defaults to the last 30 days of configuration records."
        ),
    )
    async def oura_ring_info(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_ring_info", Resource.RING_CONFIGURATION, start_date, end_date, default_days=30
        )
