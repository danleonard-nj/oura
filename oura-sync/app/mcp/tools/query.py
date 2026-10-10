"""The escape hatch for collections without a dedicated tool."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.tools.common import EndDate, StartDate, ToolRunner
from app.models.resources import Resource
from app.models.results import RecordSet

#: Resources reachable through `oura_query` but with no tool of their own.
#: Keeps the long tail available without another dozen schemas.
QUERYABLE = tuple(r.value for r in Resource)


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_query",
        title="Oura raw collection query",
        description=(
            "Fetch any Oura v2 collection by name. Use this only for collections the named "
            "tools do not cover: 'sleep' (raw per-period sleep sessions behind the daily "
            "score), 'sleep_time' (bedtime guidance), 'tag' (legacy tag format), "
            "'rest_mode_period' and 'ring_battery_level'. For everything else prefer the "
            "dedicated tool, which has a better-tuned default range."
        ),
    )
    async def oura_query(
        resource: Annotated[
            str,
            Field(description=f"Oura v2 collection name. One of: {', '.join(QUERYABLE)}."),
        ],
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> RecordSet:
        try:
            selected = Resource(resource)
        except ValueError as exc:
            raise ToolError(
                f"Unknown resource {resource!r}. Valid values: {', '.join(QUERYABLE)}."
            ) from exc

        if selected is Resource.PERSONAL_INFO:
            raise ToolError("Use oura_personal_info for personal_info; it takes no date range.")

        return await runner.records("oura_query", selected, start_date, end_date, default_days=7)
