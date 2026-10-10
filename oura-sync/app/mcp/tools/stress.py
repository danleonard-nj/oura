"""Stress and resilience: how load is being absorbed."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_stress",
        title="Oura stress and recovery",
        description=(
            "Daily stress summaries: minutes classified as high stress versus recovery, and an "
            "overall day summary such as restored, normal or stressful. Defaults to the last "
            "7 days."
        ),
    )
    async def oura_stress(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_stress", Resource.DAILY_STRESS, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_resilience",
        title="Oura resilience",
        description=(
            "Daily resilience level (for example solid, strong, exceptional) with its sleep "
            "recovery, daytime recovery and stress contributors. A longer-horizon view of how "
            "well the user is absorbing load. Defaults to the last 14 days."
        ),
    )
    async def oura_resilience(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_resilience", Resource.DAILY_RESILIENCE, start_date, end_date, default_days=14
        )
