"""Sleep."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_sleep",
        title="Oura sleep",
        description=(
            "Daily sleep summaries and sleep scores: total and time in each stage, efficiency, "
            "latency, restfulness, average heart rate and HRV. One record per night. "
            "Defaults to the last 7 days. For the raw per-period sleep sessions behind these "
            "scores, use oura_query with resource='sleep'."
        ),
    )
    async def oura_sleep(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_sleep", Resource.DAILY_SLEEP, start_date, end_date, default_days=7
        )
