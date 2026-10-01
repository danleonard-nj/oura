"""Gateway auth for the sync-trigger and snapshot-query routes.

Both are called by other machines, never a browser: the external C# cron
scheduler hits `/internal/sync/run`, and oura-mcp hits `/api/snapshots/*`.
Both go through the same Azure AD client-credentials pattern the other
internal services use -- see `oura_common.azure_ad`.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from oura_common.azure_ad import AzureAdError, verify_bearer_token

from oura_sync.api.dependencies import json_error, resolve
from oura_sync.config import Config


def require_gateway_token(request: Request) -> JSONResponse | None:
    """Gate a route behind an Azure AD bearer token. Returns an error response or None."""
    config = resolve(request, Config)

    if not config.aad_tenant_id or not config.aad_audience:
        return json_error(
            "This deployment has no AAD_TENANT_ID/AAD_AUDIENCE configured, so "
            "this route cannot verify a caller and always refuses.",
            503,
            error="not_configured",
        )

    try:
        verify_bearer_token(
            request.headers.get("authorization"),
            tenant_id=config.aad_tenant_id,
            audience=config.aad_audience,
            required_role=config.aad_required_role,
        )
    except AzureAdError as exc:
        return json_error(str(exc), 401, error="unauthorized")

    return None
