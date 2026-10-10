"""Stored history: everything the sync has archived, read from Mongo."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.tools.common import ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet
from app.services.history_service import DEFAULT_RECORD_LIMIT, MAX_LIMIT, HistoryService
from app.services.oura_service import OuraService

RESOURCES = ", ".join(r.value for r in Resource)


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_resource_history",
        title="Oura stored history",
        description=(
            "Long-range Oura history from this server's own archive rather than the live API. "
            "A sync copies every Oura record here every few hours and back-fills everything "
            "older, so this reaches data Oura itself no longer returns (ring battery samples "
            "especially) and spans years in one call without Oura's range limits. Two "
            "resources are kept as a log of changes instead of records: ring_configuration "
            "(answers 'when did my firmware update') and personal_info (profile changes). "
            "Each record carries Oura's document under `payload` and when it happened as "
            "`observed_at`. Prefer the live tools for recent days, since the archive can lag "
            "the ring by a few hours. If a window holds more than `limit` records, the most "
            "recent ones are returned."
        ),
    )
    async def oura_resource_history(
        resource: Annotated[str, Field(description=f"One of: {RESOURCES}.")],
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
        limit: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=MAX_LIMIT,
                description=f"Most records to return. Defaults to {DEFAULT_RECORD_LIMIT} (100 for change logs).",
            ),
        ] = None,
    ) -> RecordSet:
        try:
            selected = Resource(resource)
        except ValueError as exc:
            raise ToolError(f"Unknown resource {resource!r}. Valid values: {RESOURCES}.") from exc

        async def operation(_oura: OuraService):
            history = await runner.resolve(HistoryService).get_history(
                selected, since=since, until=until, limit=limit
            )
            return history, None

        history, _ = await runner.run("oura_resource_history", operation, resource=selected)
        return RecordSet(
            resource=selected.value,
            count=len(history),
            cache="bypass",
            records=history,
        )
