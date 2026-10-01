"""The ring itself: configuration, firmware and battery history."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet
from app.services.history_service import TRACKED_RESOURCES, HistoryService
from app.services.oura_service import OuraService


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

    @server.tool(
        name="oura_resource_history",
        title="Oura ring/profile history",
        description=(
            "Historical snapshots of things Oura's live API only reports current state for: "
            "ring configuration and firmware (resource='ring_configuration'), ring battery "
            "level over time (resource='ring_battery_level'), and profile changes "
            "(resource='personal_info'). Backed by a sync that polls these on a cadence "
            "and stores only the snapshots that changed, so this can answer 'when did my "
            "firmware update' or 'chart my battery level this month' in a way the live Oura "
            "API cannot. Not available for any other resource -- use the dedicated tool or "
            "oura_query for those."
        ),
    )
    async def oura_resource_history(
        resource: Annotated[
            str,
            Field(description=f"One of: {', '.join(r.value for r in TRACKED_RESOURCES)}."),
        ],
        since: Annotated[
            str | None,
            Field(
                default=None,
                description="Inclusive lower bound, ISO-8601 YYYY-MM-DD. Defaults to all history.",
            ),
        ] = None,
        until: Annotated[
            str | None,
            Field(default=None, description="Inclusive upper bound, ISO-8601 YYYY-MM-DD. Defaults to now."),
        ] = None,
    ) -> RecordSet:
        try:
            selected = Resource(resource)
        except ValueError as exc:
            raise ToolError(
                f"Unknown resource {resource!r}. Valid values: "
                f"{', '.join(r.value for r in TRACKED_RESOURCES)}."
            ) from exc

        if selected not in TRACKED_RESOURCES:
            raise ToolError(
                f"{resource!r} has no tracked history. Valid values: "
                f"{', '.join(r.value for r in TRACKED_RESOURCES)}."
            )

        async def operation(_oura: OuraService):
            snapshots = await runner.resolve(HistoryService).get_snapshots(
                selected, since=since, until=until
            )
            return snapshots, None

        snapshots, _ = await runner.run("oura_resource_history", operation, resource=selected)
        return RecordSet(
            resource=selected.value,
            count=len(snapshots),
            cache="bypass",
            records=snapshots,
        )
