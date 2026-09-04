"""Oura OAuth2 endpoints, async."""

from __future__ import annotations

from urllib.parse import urlencode

import httpx2
from httpx2 import AsyncClient

from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import get_logger
from oura_mcp.models.errors import TokenRefreshError
from oura_mcp.models.tokens import TokenGrant

logger = get_logger(__name__)

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"


class OuraOAuthClient:
    """Authorization Code flow against Oura's OAuth2 endpoints."""

    def __init__(self, http: AsyncClient, config: Config) -> None:
        self._http = http
        self._client_id = config.oura_client_id
        self._client_secret = config.oura_client_secret.get_secret_value()
        self._redirect_uri = config.oura_redirect_uri
        self._scopes = config.oura_scopes
        self._timeout = config.oura_request_timeout_seconds

    def authorization_url(self, state: str) -> str:
        """Build the URL the user's browser is sent to."""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scopes,
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str) -> TokenGrant:
        """Trade an authorization code for a token pair."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            },
            operation="authorization_code",
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """Trade a refresh token for a new pair.

        Oura refresh tokens are single-use: the returned pair replaces both
        halves, and the token passed in is dead once this returns.
        """
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            operation="refresh_token",
        )

    async def _token_request(self, payload: dict[str, str], *, operation: str) -> TokenGrant:
        body = payload | {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = await self._http.post(TOKEN_URL, data=body, timeout=self._timeout)
        except httpx2.HTTPError as exc:
            raise TokenRefreshError(
                f"Could not reach the Oura token endpoint during {operation}: "
                f"{type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            # Oura returns {"error": ..., "error_description": ...} here. Only
            # the error code is safe to surface; the body can echo the grant
            # that was submitted.
            error_code = "unknown_error"
            try:
                error_code = str(response.json().get("error", error_code))
            except ValueError:
                pass
            logger.warning(
                "oura token request rejected",
                extra={
                    "operation": operation,
                    "upstream_status": response.status_code,
                    "oura_error": error_code,
                },
            )
            raise TokenRefreshError(
                f"Oura rejected the {operation} grant "
                f"(HTTP {response.status_code}, error={error_code}). "
                "The authorization may have been revoked; reconnect the account.",
                status_code=response.status_code,
            )

        try:
            return TokenGrant.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise TokenRefreshError(
                f"Oura returned an unreadable token response for {operation}"
            ) from exc
