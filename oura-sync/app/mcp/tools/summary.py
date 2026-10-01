"""Composite day-by-day view joining every daily metric."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import Summary
from app.services.oura_service import OuraService
from app.services.summary_service import SummaryService


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_summary",
        title="Oura daily summary",
        description=(
            "Composite day-by-day view of the user's Oura data: sleep, readiness, activity, "
            "stress, resilience, blood oxygen and workouts, joined on the calendar day. "
            "Start here for questions like 'how did I sleep this week', 'how am I recovering' "
            "or 'compare my readiness to my activity'. Defaults to the last 7 days. Resources "
            "the account cannot access are listed in `unavailable` rather than failing the call."
        ),
    )
    async def oura_summary(start_date: StartDate = None, end_date: EndDate = None) -> Summary:
        async def operation(service: OuraService):
            date_range = service.resolve_range(
                Resource.DAILY_SLEEP, start_date, end_date, default_days=7
            )
            return await runner.resolve(SummaryService).build(date_range), date_range

        summary, _ = await runner.run("oura_summary", operation)
        return summary
