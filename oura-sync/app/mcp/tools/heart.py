"""Heart and cardiorespiratory metrics."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_heart_rate",
        title="Oura heart rate time series",
        description=(
            "Individual heart rate samples with their timestamp and source (awake, rest, "
            "sleep, workout). This is high volume -- expect hundreds of samples per day -- so "
            "the range is capped at 14 days and defaults to 1 day. Prefer oura_sleep or "
            "oura_readiness for average and resting heart rate."
        ),
    )
    async def oura_heart_rate(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_heart_rate", Resource.HEARTRATE, start_date, end_date, default_days=1
        )

    @server.tool(
        name="oura_spo2",
        title="Oura blood oxygen",
        description=(
            "Daily average blood oxygen saturation (SpO2) measured during sleep, plus the "
            "breathing disturbance index where available. Defaults to the last 7 days. "
            "Requires the spo2 scope and a ring generation that supports it."
        ),
    )
    async def oura_spo2(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_spo2", Resource.DAILY_SPO2, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_cardiovascular_age",
        title="Oura cardiovascular age",
        description=(
            "Daily estimated vascular age relative to the user's chronological age. Changes "
            "very slowly, so a wide range is normal. Defaults to the last 30 days."
        ),
    )
    async def oura_cardiovascular_age(
        start_date: StartDate = None, end_date: EndDate = None
    ) -> RecordSet:
        return await runner.records(
            "oura_cardiovascular_age",
            Resource.DAILY_CARDIOVASCULAR_AGE,
            start_date,
            end_date,
            default_days=30,
        )

    @server.tool(
        name="oura_vo2_max",
        title="Oura VO2 max",
        description=(
            "Estimated VO2 max (cardiorespiratory fitness) readings. Produced only when the "
            "user completes a walking test, so records are sparse. Defaults to the last 90 days."
        ),
    )
    async def oura_vo2_max(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await runner.records(
            "oura_vo2_max", Resource.VO2_MAX, start_date, end_date, default_days=90
        )
