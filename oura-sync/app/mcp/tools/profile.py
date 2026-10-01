"""The user profile."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp.tools.common import ToolRunner
from app.models.resources import Resource
from app.models.results import Document
from app.services.oura_service import OuraService


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_personal_info",
        title="Oura personal info",
        description=(
            "The user's profile: age, biological sex, height and weight. Needed to interpret "
            "metrics that are relative to body composition. Takes no date range."
        ),
    )
    async def oura_personal_info() -> Document:
        async def operation(service: OuraService):
            return await service.get_personal_info(), None

        fetch, _ = await runner.run(
            "oura_personal_info", operation, resource=Resource.PERSONAL_INFO
        )
        return Document(
            resource=Resource.PERSONAL_INFO.value,
            cache=fetch.cache.value,
            document=fetch.records,
        )
