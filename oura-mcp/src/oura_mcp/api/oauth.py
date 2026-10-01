"""OAuth2 authorization and callback endpoints.

Neither endpoint ever returns token material. The callback confirms the
connection and reports the granted scopes; the tokens themselves go straight
into encrypted storage.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from oura_common.infrastructure.logging import get_logger
from oura_common.models.errors import TokenRefreshError
from oura_common.services.oauth_service import OAuthService, OAuthStateError

from oura_mcp.api.dependencies import json_error, resolve

logger = get_logger(__name__)


async def authorize(request: Request) -> RedirectResponse | JSONResponse:
    """Begin the flow by redirecting the browser to Oura.

    Pass `?json=1` to receive the URL instead of a redirect, which is what the
    oura-auth bootstrap tool uses.
    """
    service = resolve(request, OAuthService)
    try:
        url = await service.start()
    except OAuthStateError as exc:
        return json_error(str(exc), 503, error="oauth_unavailable")

    if request.query_params.get("json"):
        return JSONResponse({"authorization_url": url})
    return RedirectResponse(url, status_code=302)


async def callback(request: Request) -> JSONResponse:
    """Complete the flow with the authorization code Oura redirected back."""
    params = request.query_params

    if error := params.get("error"):
        # Oura puts the reason in the query string; the code itself is never
        # logged, and neither is anything else from this request.
        logger.warning("oauth callback returned an error", extra={"oauth_error": error})
        return json_error(
            f"Oura declined the authorization request: {error}", 400, error="oauth_denied"
        )

    code = params.get("code")
    if not code:
        return json_error("The callback did not include an authorization code.", 400)

    service = resolve(request, OAuthService)
    try:
        connection = await service.complete(code, params.get("state", ""))
    except OAuthStateError as exc:
        return json_error(str(exc), 400, error="invalid_state")
    except TokenRefreshError as exc:
        return json_error(exc.message, 502, error="token_exchange_failed")

    return JSONResponse(
        {
            "status": "connected",
            "account_id": connection.account_id,
            "scopes": connection.scopes,
            "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
        }
    )


ROUTES = [
    Route("/oauth/authorize", authorize, methods=["GET"]),
    Route("/oauth/callback", callback, methods=["GET"]),
]
