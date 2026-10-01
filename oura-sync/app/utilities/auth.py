"""Route guards for the two kinds of machine caller this service has.

* `require_api_key` -- the administrative `/api/*` routes, behind the same
  static `mcp.api_key` an MCP client sends. One shared key, compared in
  constant time.
* `require_gateway_token` -- `/internal/sync/run` and `/api/snapshots/*`,
  behind an Azure AD client-credentials token (the external cron scheduler),
  the gateway pattern the other internal services use.

The MCP endpoint itself is guarded by framework's `BearerAuth` (see
`app.mcp.app`), which also accepts the OAuth access tokens this service
issues. Health probes, the Oura OAuth callback and the Oura webhook each have
their own verification model and use neither.
"""

from __future__ import annotations

import hmac
from functools import wraps
from typing import Awaitable, Callable

from quart import request

from app.models.config import GatewayAuthConfig, McpConfig
from app.utilities.azure_ad import AzureAdError, verify_bearer_token
from app.utilities.http import json_error
from app.utilities.provider import resolve

View = Callable[..., Awaitable]


def bearer_credential(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def api_key_matches(authorization: str | None, api_key: str) -> bool:
    credential = bearer_credential(authorization)
    if credential is None:
        return False
    return hmac.compare_digest(credential.encode(), api_key.encode())


def require_api_key(view: View) -> View:
    """Gate an administrative route behind `mcp.api_key`."""

    @wraps(view)
    async def wrapper(*args, **kwargs):
        if not api_key_matches(
            request.headers.get("authorization"), resolve(McpConfig).api_key.get_secret_value()
        ):
            response = json_error("A valid bearer token is required.", 401, error="unauthorized")
            response.headers["WWW-Authenticate"] = 'Bearer realm="oura"'
            return response
        return await view(*args, **kwargs)

    return wrapper


def require_gateway_token(view: View) -> View:
    """Gate a route behind an Azure AD bearer token issued for this service."""

    @wraps(view)
    async def wrapper(*args, **kwargs):
        config = resolve(GatewayAuthConfig)

        if not config.configured:
            return json_error(
                "This deployment has no auth.tenant_id/auth.audiences configured, so "
                "this route cannot verify a caller and always refuses.",
                503,
                error="not_configured",
            )

        try:
            verify_bearer_token(
                request.headers.get("authorization"),
                tenant_id=config.tenant_id or "",
                audiences=config.audiences,
                required_role=config.required_role,
            )
        except AzureAdError as exc:
            return json_error(str(exc), 401, error="unauthorized")

        return await view(*args, **kwargs)

    return wrapper
