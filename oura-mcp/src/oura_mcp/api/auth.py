"""Bearer-token authentication for the MCP endpoint.

This is a deliberately small, self-contained layer for a personal deployment:
one shared key, compared in constant time, checked before the request reaches
the MCP app. It is an ASGI middleware wrapping only the MCP app, so health
probes, the OAuth callback and the Oura webhook -- each of which has its own
verification model -- are untouched.

Replacing it with standards-based MCP OAuth later means swapping this wrapper
for the SDK's `token_verifier`; nothing else in the application knows it exists.
"""

from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from oura_mcp.infrastructure.logging import (
    get_logger,
    new_correlation_id,
    set_correlation_id,
)

logger = get_logger(__name__)

CORRELATION_HEADER = "x-correlation-id"


class BearerTokenMiddleware:
    """Requires `Authorization: Bearer <key>` on every wrapped request."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        if not api_key:
            raise ValueError("MCP_API_KEY must be set; refusing to expose /mcp unauthenticated.")
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(CORRELATION_HEADER) or new_correlation_id()
        set_correlation_id(correlation_id)

        if not self._authorized(headers.get("authorization")):
            logger.warning(
                "mcp request rejected: bad or missing bearer token",
                extra={"correlation_id": correlation_id, "path": scope.get("path")},
            )
            response = JSONResponse(
                {"error": "unauthorized", "detail": "A valid bearer token is required."},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="oura-mcp"',
                    CORRELATION_HEADER: correlation_id,
                },
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)

    def _authorized(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return False
        return hmac.compare_digest(credential.strip(), self._api_key)


def require_api_key(request_authorization: str | None, api_key: str) -> bool:
    """Same check, for the administrative HTTP routes."""
    if not request_authorization:
        return False
    scheme, _, credential = request_authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential.strip(), api_key)
