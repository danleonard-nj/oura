"""OAuth2 authorization and callback endpoints.

Neither endpoint ever returns token material. The callback confirms the
connection and reports the granted scopes; the tokens themselves go straight
into encrypted storage.
"""

from __future__ import annotations

from depi_quart import QuartInjector
from quart import Blueprint, redirect, request

from app.models.errors import TokenRefreshError
from app.services.oauth_service import OAuthService, OAuthStateError
from app.utilities.http import json_error, json_response
from app.utilities.logging import get_logger

logger = get_logger(__name__)


def create_oauth_blueprint(injector: QuartInjector) -> Blueprint:
    oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")

    @oauth_bp.route("/authorize", methods=["GET"])
    @injector.inject
    async def authorize(service: OAuthService):
        """Begin the flow by redirecting the browser to Oura.

        Pass `?json=1` to receive the URL instead of a redirect, which is what the
        oura-auth bootstrap tool uses.
        """
        try:
            url = await service.start()
        except OAuthStateError as exc:
            return json_error(str(exc), 503, error="oauth_unavailable")

        if request.args.get("json"):
            return json_response({"authorization_url": url})
        return redirect(url, 302)

    @oauth_bp.route("/callback", methods=["GET"])
    @injector.inject
    async def callback(service: OAuthService):
        """Complete the flow with the authorization code Oura redirected back."""
        params = request.args

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

        try:
            connection = await service.complete(code, params.get("state", ""))
        except OAuthStateError as exc:
            return json_error(str(exc), 400, error="invalid_state")
        except TokenRefreshError as exc:
            return json_error(exc.message, 502, error="token_exchange_failed")

        return json_response(
            {
                "status": "connected",
                "account_id": connection.account_id,
                "scopes": connection.scopes,
                "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
            }
        )

    return oauth_bp
