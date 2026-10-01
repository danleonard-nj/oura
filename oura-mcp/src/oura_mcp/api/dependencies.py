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

from oura_mcp.api.auth import CORRELATION_HEADER, require_api_key
from oura_mcp.config import Config
from oura_mcp.models.request_log import RequestLogEntry
from oura_mcp.repositories.request_log_repository import RequestLogRepository

T = TypeVar("T")

_access_logger = get_logger("oura_mcp.access")

# Kubelet probes hit these every few seconds. They're noise once the app is
# known to be up, so they log at DEBUG instead of crowding out real traffic.
_QUIET_PATHS = frozenset({"/health", "/ready"})


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

    This also emits the request's access log line. Hypercorn's own
    `--access-logfile` writes a separate, non-JSON stream that carries no
    correlation id, so a request could never be tied back to the audit trail
    it produced; logging it here instead keeps every line in the same
    structured format and tagged with the same correlation id as the
    `oura_request`/`tool_invoked` rows it corresponds to.

    Every non-probe request is also persisted to `request_log` (see
    `RequestLogRepository`) so it can be queried later -- e.g. to see what a
    scanner hammering the ingress actually hit. That write is best-effort: a
    Mongo hiccup here must never fail the request it is merely describing.
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

        started = time.perf_counter()
        response = {"status": None, "bytes": 0}

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                response["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            path = scope.get("path", "")
            method = scope.get("method")
            is_quiet = path in _QUIET_PATHS
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            client_ip = _client_ip(scope)
            user_agent = _header(scope, "user-agent")

            _access_logger.log(
                logging.DEBUG if is_quiet else logging.INFO,
                f"{method} {path} {response['status']}",
                extra={
                    "method": method,
                    "path": path,
                    "status": response["status"],
                    "duration_ms": duration_ms,
                    "bytes": response["bytes"],
                    "client_ip": client_ip,
                    "user_agent": user_agent,
                },
            )

            if not is_quiet:
                await _record_request_log(
                    scope,
                    RequestLogEntry(
                        correlation_id=correlation_id,
                        method=method or "",
                        path=path,
                        status=response["status"],
                        duration_ms=duration_ms,
                        bytes=response["bytes"],
                        client_ip=client_ip,
                        user_agent=user_agent,
                    ),
                )


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode()
    return None


def _client_ip(scope: Scope) -> str | None:
    """Best-effort real client address, seen through the ingress proxy.

    `scope["client"]` is the raw ASGI socket peer, which behind our
    ingress-nginx deployment is always the ingress pod itself (a
    cluster-internal 10.x address) — never the actual caller. We trust
    exactly one hop of proxying (the ingress), so `X-Real-IP`, which nginx
    sets to the immediate connecting address, is authoritative when present.
    Failing that, take the rightmost hop of `X-Forwarded-For`: nginx appends
    the address it saw to whatever value the client sent, so anything a
    client supplies itself lands to the left of that and is not trustworthy.
    """
    real_ip = _header(scope, "x-real-ip")
    if real_ip:
        return real_ip.strip()

    forwarded_for = _header(scope, "x-forwarded-for")
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1]

    client = scope.get("client") or (None, None)
    return client[0]


async def _record_request_log(scope: Scope, entry: RequestLogEntry) -> None:
    """Persist one `RequestLogEntry`, tolerating a missing or unhealthy Mongo.

    Startup has not necessarily finished the first time this runs -- ASGI
    lifespan and request handling can interleave in some servers -- so a
    missing provider is expected, not an error; anything else is a Mongo
    problem the request itself must not fail on.
    """
    provider: ServiceProvider | None = getattr(
        getattr(scope.get("app"), "state", None), "provider", None
    )
    if provider is None:
        return
    try:
        await provider.resolve(RequestLogRepository).record(entry)
    except Exception as exc:  # noqa: BLE001 - request logging must never fail a request
        _access_logger.warning(
            "request log persistence failed",
            extra={"correlation_id": entry.correlation_id, "error": str(exc)},
        )


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
