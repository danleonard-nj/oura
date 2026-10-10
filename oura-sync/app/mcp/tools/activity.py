"""Activity, workouts, sessions and user-entered tags."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_activity",
        title="Oura activity",
        description=(
            "Daily activity summaries: steps, active and total calories, equivalent walking "
            "distance, MET minutes by intensity, sedentary and inactive time, and the activity "
            "score. Defaults to the last 7 days."
        ),
    )
    async def oura_activity(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_activity", Resource.DAILY_ACTIVITY, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_workouts",
        title="Oura workouts",
        description=(
            "Workouts, both auto-detected and manually entered: activity type, intensity, "
            "calories, distance, and start and end times. Defaults to the last 14 days."
        ),
    )
    async def oura_workouts(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_workouts", Resource.WORKOUT, start_date, end_date, default_days=14
        )

    @server.tool(
        name="oura_sessions",
        title="Oura sessions",
        description=(
            "Guided and unguided sessions such as meditation, breathing and naps, with their "
            "heart rate, HRV and motion series where recorded. Defaults to the last 14 days."
        ),
    )
    async def oura_sessions(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_sessions", Resource.SESSION, start_date, end_date, default_days=14
        )

    @server.tool(
        name="oura_tags",
        title="Oura tags",
        description=(
            "User-entered tags and notes -- caffeine, alcohol, illness, travel and so on -- "
            "useful for correlating behaviour with sleep and readiness. Returns Oura's enhanced "
            "tag format, which carries start and end times and comments. Defaults to the last "
            "14 days."
        ),
    )
    async def oura_tags(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_tags", Resource.ENHANCED_TAG, start_date, end_date, default_days=14
        )
