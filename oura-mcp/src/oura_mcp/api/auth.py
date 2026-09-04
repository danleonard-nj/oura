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
from collections.abc import Awaitable, Callable

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
    """Requires a bearer credential on every wrapped request.

    Two credentials are accepted, because MCP clients are split on what they
    can send:

    * the static `MCP_API_KEY`, for clients that let you set a header
      (Claude Code), and
    * an OAuth access token issued by this server, for clients that only do
      OAuth discovery (ChatGPT).

    The static key is checked first and costs nothing. An OAuth token needs a
    store lookup, so it is only attempted when the key does not match, and only
    when OAuth is configured.

    On failure the `WWW-Authenticate` challenge points at the protected-resource
    metadata, which is how an OAuth client discovers where to authorize.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str,
        token_loader: Callable[[str], Awaitable[object | None]] | None = None,
        resource_metadata_url: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MCP_API_KEY must be set; refusing to expose /mcp unauthenticated.")
        self._app = app
        self._api_key = api_key
        self._token_loader = token_loader
        self._resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(CORRELATION_HEADER) or new_correlation_id()
        set_correlation_id(correlation_id)

        if not await self._authorized(headers.get("authorization")):
            logger.warning(
                "mcp request rejected: bad or missing bearer credential",
                extra={"correlation_id": correlation_id, "path": scope.get("path")},
            )
            response = JSONResponse(
                {"error": "unauthorized", "detail": "A valid bearer token is required."},
                status_code=401,
                headers={
                    "WWW-Authenticate": self._challenge(),
                    CORRELATION_HEADER: correlation_id,
                },
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)

    def _challenge(self) -> str:
        challenge = 'Bearer realm="oura-mcp"'
        if self._resource_metadata_url:
            challenge += f', resource_metadata="{self._resource_metadata_url}"'
        return challenge

    async def _authorized(self, authorization: str | None) -> bool:
        credential = _bearer_credential(authorization)
        if credential is None:
            return False

        if hmac.compare_digest(credential, self._api_key):
            return True

        if self._token_loader is None:
            return False
        return await self._token_loader(credential) is not None


def _bearer_credential(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def require_api_key(request_authorization: str | None, api_key: str) -> bool:
    """Same check, for the administrative HTTP routes."""
    if not request_authorization:
        return False
    scheme, _, credential = request_authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential.strip(), api_key)
