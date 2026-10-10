"""Clearing cached Oura responses when the ring has synced since the last read."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.tools.common import ToolRunner
from app.models.resources import Resource
from app.models.results import CacheCleared
from app.services.cache_service import CacheService
from app.services.oura_service import OuraService

RESOURCES = ", ".join(r.value for r in Resource)


def register(server: MCPServer, runner: ToolRunner) -> None:
    @server.tool(
        name="oura_clear_cache",
        title="Clear Oura cache",
        description=(
            "Drop this server's cached Oura responses so the next tool call reads live from "
            "Oura. Use it when a result came back with cache 'hit' but looks stale, e.g. the "
            "ring has synced since and the same records keep coming back. Clearing covers "
            "every date range for a resource, so no dates are needed; call it, then repeat "
            "the original tool call. Omit `resources` to clear everything."
        ),
    )
    async def oura_clear_cache(
        resources: Annotated[
            list[str] | None,
            Field(
                default=None,
                description=(
                    "Resource names to clear, as reported in a result's `resource` field. "
                    f"One or more of: {RESOURCES}. Defaults to all."
                ),
            ),
        ] = None,
    ) -> CacheCleared:
        try:
            selected = [Resource(r) for r in resources] if resources else list(Resource)
        except ValueError as exc:
            raise ToolError(f"Unknown resource in {resources!r}. Valid values: {RESOURCES}.") from exc

        async def operation(oura: OuraService):
            cache = runner.resolve(CacheService)
            result = CacheCleared(cleared=[])
            for resource in dict.fromkeys(selected):
                if await cache.invalidate(oura.account_id, resource):
                    result.cleared.append(resource.value)
                else:
                    result.failed.append(resource.value)
            return result, None

        result, _ = await runner.run("oura_clear_cache", operation)
        return result
