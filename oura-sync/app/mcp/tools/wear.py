"""Whether the ring is being worn: the polled status and its history."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from app.mcp.tools.common import ToolRunner
from app.models.wear import WearHistory, WearState
from app.services.oura_service import OuraService
from app.services.wear_service import DEFAULT_HISTORY_DAYS, WearService, history_window


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_wear_status",
        title="Oura ring wear status",
        description=(
            "Whether the user is wearing their ring: WORN, NOT_WORN or UNKNOWN, with confidence, "
            "when the current period began (`since`), the last time it was known to be worn, and "
            "how old the evidence is. Judged on a schedule (every 15 minutes by default) from "
            "Oura's 5-minute activity classification and recent heart rate. This is NOT live: "
            "Oura only has what the ring "
            "last synced through the phone, so the answer is as of `latest_evidence_at`, and it "
            "reads UNKNOWN when that is too old to say anything about now."
        ),
    )
    async def oura_wear_status() -> WearState:
        async def operation(_oura: OuraService):
            return await runner.resolve(WearService).current(), None

        state, _ = await runner.run("oura_wear_status", operation)
        return state

    @server.tool(
        name="oura_wear_intervals",
        title="Oura ring wear history",
        description=(
            "When the ring was worn and when it was off, as intervals, plus worn / not-worn minute "
            "totals and the removals and put-ons the poller detected. Built from Oura's 5-minute "
            "activity classification, with short non-wear blips (under 15 minutes by default) "
            "counted as worn. `covered_through` is where Oura's data ends -- nothing after it is "
            "known yet. "
            f"Defaults to the last {DEFAULT_HISTORY_DAYS} days."
        ),
    )
    async def oura_wear_intervals(
        since: Annotated[
            str | None,
            Field(default=None, description="Inclusive lower bound, ISO-8601 date or datetime."),
        ] = None,
        until: Annotated[
            str | None,
            Field(default=None, description="Inclusive upper bound, ISO-8601 date or datetime. Defaults to now."),
        ] = None,
    ) -> WearHistory:
        async def operation(_oura: OuraService):
            lower, upper = history_window(since, until)
            return await runner.resolve(WearService).history(lower, upper), None

        history, _ = await runner.run("oura_wear_intervals", operation)
        return history
