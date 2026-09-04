"""How MCP tools reach their services.

Tool functions are registered when the ASGI app is built, but the DI provider is
not constructed until the lifespan runs inside the serving event loop -- Mongo
and Redis clients bind to the loop they are created on. `ToolContext` bridges
that gap: one object, passed to the tool registrar by constructor, populated
once at startup.
"""

from __future__ import annotations

from typing import TypeVar

from depi import ServiceProvider

T = TypeVar("T")


class ToolContext:
    """Holds the provider that MCP tools resolve their services from."""

    def __init__(self) -> None:
        self._provider: ServiceProvider | None = None

    def bind(self, provider: ServiceProvider) -> None:
        self._provider = provider

    def resolve(self, service_type: type[T]) -> T:
        if self._provider is None:
            raise RuntimeError(
                "The service provider has not been built yet. MCP tools cannot run "
                "before application startup completes."
            )
        return self._provider.resolve(service_type)
