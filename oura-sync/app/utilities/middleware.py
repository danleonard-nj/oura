"""Correlation ids, the access log and the persisted request log.

Plain ASGI middleware wrapped around the Quart app, so it sees every request
-- including MCP calls, which framework's MCPBlueprint dispatches to the SDK's
own ASGI app from inside a Quart view.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.models.request_log import RequestLogEntry
from app.repositories.request_log_repository import RequestLogRepository
from app.utilities.logging import get_logger, new_correlation_id, set_correlation_id
from app.utilities.provider import ContainerProvider

CORRELATION_HEADER = "x-correlation-id"

_access_logger = get_logger("oura.access")

# Kubelet probes hit these every few seconds. They're noise once the app is
# known to be up, so they log at DEBUG instead of crowding out real traffic.
_QUIET_PATHS = frozenset({"/health", "/ready"})


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

    def __init__(self, app: ASGIApp, container: ContainerProvider) -> None:
        self._app = app
        self._container = container

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
                await self._record_request_log(
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

    async def _record_request_log(self, entry: RequestLogEntry) -> None:
        """Persist one `RequestLogEntry`, tolerating a missing or unhealthy Mongo.

        A request can arrive before `before_serving` has bound the provider, so
        a missing provider is expected, not an error; anything else is a Mongo
        problem the request itself must not fail on.
        """
        if not self._container.ready:
            return
        try:
            await self._container.resolve(RequestLogRepository).record(entry)
        except Exception as exc:  # noqa: BLE001 - request logging must never fail a request
            _access_logger.warning(
                "request log persistence failed",
                extra={"correlation_id": entry.correlation_id, "error": str(exc)},
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


