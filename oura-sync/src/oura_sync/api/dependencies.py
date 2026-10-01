"""Service resolution and cross-cutting concerns for HTTP routes."""

from __future__ import annotations

import logging
import time
from typing import TypeVar

from depi import ServiceProvider
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from oura_common.infrastructure.logging import (
    get_correlation_id,
    get_logger,
    new_correlation_id,
    set_correlation_id,
)

T = TypeVar("T")

CORRELATION_HEADER = "x-correlation-id"

_access_logger = get_logger("oura_sync.access")

# Probes hit these every few seconds; noise once the app is known to be up.
_QUIET_PATHS = frozenset({"/health", "/ready"})


def resolve(request: Request, service_type: type[T]) -> T:
    """Resolve a service from the provider built during application startup."""
    provider: ServiceProvider | None = getattr(request.app.state, "provider", None)
    if provider is None:
        raise RuntimeError("Application startup has not completed; no service provider.")
    return provider.resolve(service_type)


class CorrelationIdMiddleware:
    """Gives every HTTP request a correlation id, echoes it back, and logs it."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        inbound = _header(scope, CORRELATION_HEADER)
        correlation_id = inbound or new_correlation_id()
        set_correlation_id(correlation_id)

        started = time.perf_counter()
        response = {"status": None}

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            path = scope.get("path", "")
            is_quiet = path in _QUIET_PATHS
            _access_logger.log(
                logging.DEBUG if is_quiet else logging.INFO,
                f"{scope.get('method')} {path} {response['status']}",
                extra={
                    "method": scope.get("method"),
                    "path": path,
                    "status": response["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode()
    return None


def json_error(message: str, status_code: int, *, error: str = "error") -> JSONResponse:
    return JSONResponse(
        {"error": error, "detail": message, "correlation_id": get_correlation_id()},
        status_code=status_code,
    )
