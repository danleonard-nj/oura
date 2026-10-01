"""Readiness."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_readiness",
        title="Oura readiness",
        description=(
            "Daily readiness scores and their contributors: resting heart rate, HRV balance, "
            "body temperature deviation, recovery index, sleep balance and activity balance. "
            "Use for 'am I recovered', 'should I train today' and illness-onset questions. "
            "Defaults to the last 7 days."
        ),
    )
    async def oura_readiness(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_readiness", Resource.DAILY_READINESS, start_date, end_date, default_days=7
        )
