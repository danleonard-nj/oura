"""Service resolution and cross-cutting concerns for HTTP routes."""

from __future__ import annotations

from typing import TypeVar

from depi import ServiceProvider
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from oura_mcp.api.auth import CORRELATION_HEADER, require_api_key
from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)

T = TypeVar("T")


def resolve(request: Request, service_type: type[T]) -> T:
    """Resolve a service from the provider built during application startup."""
    provider: ServiceProvider | None = getattr(request.app.state, "provider", None)
    if provider is None:
        raise RuntimeError("Application startup has not completed; no service provider.")
    return provider.resolve(service_type)


class CorrelationIdMiddleware:
    """Gives every HTTP request a correlation id and echoes it back.

    An inbound `x-correlation-id` is honoured so a caller can stitch its own
    trace to ours; otherwise one is minted. The id flows into structured logs,
    Oura request logging and Mongo audit records via a context variable.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        inbound = _header(scope, CORRELATION_HEADER)
        correlation_id = inbound or new_correlation_id()
        set_correlation_id(correlation_id)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_header)


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode()
    return None


def unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": "A valid bearer token is required."},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="oura-mcp"'},
    )


def admin_guard(request: Request) -> JSONResponse | None:
    """Gate an administrative route behind MCP_API_KEY. Returns a 401 or None."""
    config = resolve(request, Config)
    if require_api_key(request.headers.get("authorization"), config.mcp_api_key.get_secret_value()):
        return None
    return unauthorized()


def json_error(message: str, status_code: int, *, error: str = "error") -> JSONResponse:
    return JSONResponse(
        {"error": error, "detail": message, "correlation_id": get_correlation_id()},
        status_code=status_code,
    )
