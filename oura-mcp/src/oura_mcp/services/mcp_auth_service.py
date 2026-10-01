"""OAuth 2.1 authorization server for the MCP endpoint.

Some MCP clients -- ChatGPT among them -- offer only OAuth or no auth, with no
field for a static header. Serving OAuth is the only way to reach them without
leaving health data unauthenticated on the public internet.

The SDK provides the protocol surface: `/authorize`, `/token`, `/register`,
`/revoke` and the two discovery documents. This module supplies the behaviour
behind them.

Two things are worth knowing about the shape of it:

**There is no user database.** This deployment serves one person. Rather than
inventing accounts, an authorization is approved by entering an owner secret on
a consent screen. `MCP_OWNER_PASSWORD` is compared directly, so it must be
high-entropy -- generate it like an API key.

**Dynamic client registration is open**, because ChatGPT cannot pre-register a
client_id. That is per RFC 7591 and expected by the MCP spec, and it is safe
here only because registration alone grants nothing: every authorization still
has to be approved with the owner secret.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from oura_common.infrastructure.logging import get_correlation_id, get_logger
from oura_common.models.audit import AuditAction, AuditEvent
from oura_common.services.audit_service import AuditService

from oura_mcp.config import Config
from oura_mcp.repositories.oauth_repository import OAuthRepository

logger = get_logger(__name__)

#: The single scope this server issues. Access is all-or-nothing: a client that
#: can read sleep can read readiness, and pretending otherwise would be theatre.
MCP_SCOPE = "oura:read"

CONSENT_PATH = "/consent"


class ConsentError(Exception):
    """The consent step could not be completed."""


class ConsentLockedOut(ConsentError):
    """Too many failed owner-password attempts."""


class McpAuthService(OAuthAuthorizationServerProvider):
    """Implements the SDK's authorization-server contract.

    Token values are random and opaque rather than JWTs. A JWT would let a
    replica validate without a lookup, but revocation then needs a denylist
    that costs the same lookup -- and opaque tokens cannot leak claims.
    """

    def __init__(
        self,
        repository: OAuthRepository,
        audit: AuditService,
        config: Config,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._config = config
        self._owner_password = (
            config.mcp_owner_password.get_secret_value()
            if config.mcp_owner_password
            else None
        )

    # ── Client registration ───────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        document = await self._repository.get_client(client_id)
        if document is None:
            return None
        return OAuthClientInformationFull.model_validate(document)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._repository.save_client(
            client_info.client_id,
            client_info.model_dump(mode="json", exclude_none=True),
        )
        logger.info(
            "mcp oauth client registered",
            extra={
                "oauth_client_id": client_info.client_id,
                "client_name": client_info.client_name,
                "redirect_uris": [str(uri) for uri in client_info.redirect_uris or []],
            },
        )

    # ── Authorization ─────────────────────────────────────────────────────

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the consent screen.

        Nothing is issued here. The redirect goes to our own consent page; the
        authorization code is only minted once the owner secret is accepted.
        """
        if self._owner_password is None:
            raise ConsentError(
                "MCP OAuth is not configured: MCP_OWNER_PASSWORD is unset, so no "
                "authorization can be approved."
            )

        txn_id = secrets.token_urlsafe(32)
        await self._repository.put_pending(
            txn_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [MCP_SCOPE],
                "resource": params.resource,
            },
            ttl=self._config.oauth_consent_ttl_seconds,
        )

        logger.info(
            "mcp oauth authorization pending consent",
            extra={"oauth_client_id": client.client_id, "txn": txn_id[:8]},
        )
        return f"{self._config.public_base_url}{CONSENT_PATH}?txn={txn_id}"

    async def approve_consent(self, txn_id: str, password: str, attempt_key: str) -> str:
        """Validate the owner secret and mint an authorization code.

        Returns the redirect URL back to the client, carrying `code` and the
        original `state`.
        """
        import hmac

        if self._owner_password is None:
            raise ConsentError("MCP OAuth is not configured on this server.")

        attempts = await self._repository.failed_attempts(attempt_key)
        if attempts >= self._config.oauth_consent_max_attempts:
            raise ConsentLockedOut(
                "Too many failed attempts. Wait "
                f"{self._config.oauth_consent_lockout_seconds // 60} minutes and try again."
            )

        pending = await self._repository.peek_pending(txn_id)
        if pending is None:
            raise ConsentError(
                "This authorization request has expired or was already completed. "
                "Start again from your MCP client."
            )

        if not hmac.compare_digest(password, self._owner_password):
            count = await self._repository.record_failed_attempt(
                attempt_key, ttl=self._config.oauth_consent_lockout_seconds
            )
            logger.warning(
                "mcp oauth consent rejected",
                extra={"oauth_client_id": pending["client_id"], "attempt": count},
            )
            await self._audit.record(
                AuditEvent(
                    action=AuditAction.OAUTH_AUTHORIZE,
                    account_id=self._config.oura_account_id,
                    correlation_id=get_correlation_id(),
                    status="rejected",
                    error_message="incorrect owner password",
                    detail={"oauth_client_id": pending["client_id"], "attempt": count},
                )
            )
            raise ConsentError("Incorrect owner password.")

        # Correct: consume the pending request so the page cannot be replayed.
        pending = await self._repository.take_pending(txn_id)
        if pending is None:  # pragma: no cover - lost a race with another tab
            raise ConsentError("This authorization request was already completed.")

        await self._repository.clear_failed_attempts(attempt_key)

        code = secrets.token_urlsafe(32)
        await self._repository.put_code(
            code,
            {
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending[
                    "redirect_uri_provided_explicitly"
                ],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "resource": pending.get("resource"),
                "expires_at": time.time() + self._config.oauth_authorization_code_ttl_seconds,
            },
            ttl=self._config.oauth_authorization_code_ttl_seconds,
        )

        await self._audit.record(
            AuditEvent(
                action=AuditAction.OAUTH_AUTHORIZE,
                account_id=self._config.oura_account_id,
                correlation_id=get_correlation_id(),
                detail={
                    "oauth_client_id": pending["client_id"],
                    "client_name": pending.get("client_name"),
                    "scopes": pending["scopes"],
                },
            )
        )
        logger.info(
            "mcp oauth consent granted",
            extra={"oauth_client_id": pending["client_id"]},
        )

        return _redirect_with(pending["redirect_uri"], code=code, state=pending.get("state"))

    async def pending_request(self, txn_id: str) -> dict[str, Any] | None:
        """Details for rendering the consent page. No secrets in the result."""
        pending = await self._repository.peek_pending(txn_id)
        if pending is None:
            return None
        return {
            "client_id": pending["client_id"],
            "client_name": pending.get("client_name"),
            "scopes": pending["scopes"],
            "redirect_uri": pending["redirect_uri"],
        }

    async def deny_consent(self, txn_id: str) -> str | None:
        """Abandon an authorization and send the client an `access_denied`."""
        pending = await self._repository.take_pending(txn_id)
        if pending is None:
            return None
        return _redirect_with(
            pending["redirect_uri"], error="access_denied", state=pending.get("state")
        )

    # ── Authorization code exchange ───────────────────────────────────────

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = await self._repository.peek_code(authorization_code)
        if record is None or record["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            client_id=record["client_id"],
            code_challenge=record["code_challenge"],
            redirect_uri=record["redirect_uri"],
            redirect_uri_provided_explicitly=record["redirect_uri_provided_explicitly"],
            resource=record.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Consume the code here, not at load time: the SDK verifies PKCE
        # between the two, and a code that fails verification must not have
        # been silently spent.
        consumed = await self._repository.take_code(authorization_code.code)
        if consumed is None:
            raise ValueError("Authorization code has already been used or has expired.")

        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # ── Refresh ───────────────────────────────────────────────────────────

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = await self._repository.get_refresh_token(refresh_token)
        if record is None or record["client_id"] != client.client_id:
            return None
        if record.get("expires_at") and record["expires_at"] < time.time():
            await self._repository.delete_refresh_token(refresh_token)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]) if record.get("expires_at") else None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate the refresh token along with the access token.

        The old one is deleted before the new pair is returned, so a replayed
        refresh finds nothing -- the same single-use discipline Oura applies to
        its own refresh tokens.
        """
        await self._repository.delete_refresh_token(refresh_token.token)
        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            resource=None,
        )

    # ── Access tokens ─────────────────────────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = await self._repository.get_access_token(token)
        if record is None:
            return None
        if record.get("expires_at") and record["expires_at"] < time.time():
            await self._repository.delete_access_token(token)
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]) if record.get("expires_at") else None,
            resource=record.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self._repository.delete_access_token(token.token)
        else:
            await self._repository.delete_refresh_token(token.token)
        logger.info("mcp oauth token revoked", extra={"oauth_client_id": token.client_id})

    async def _issue_tokens(
        self, *, client_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        now = time.time()
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        access_ttl = self._config.oauth_access_token_ttl_seconds

        await self._repository.put_access_token(
            access_token,
            {
                "client_id": client_id,
                "scopes": scopes,
                "resource": resource,
                "expires_at": int(now) + access_ttl,
            },
            ttl=access_ttl,
        )
        await self._repository.put_refresh_token(
            refresh_token,
            {
                "client_id": client_id,
                "scopes": scopes,
                # Whole seconds: RefreshToken.expires_at is typed int, and a
                # fractional float fails model validation on the way back out.
                "expires_at": int(now) + self._config.oauth_refresh_token_ttl_seconds,
            },
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )


def _redirect_with(redirect_uri: str, **params: str | None) -> str:
    """Append parameters to the client's redirect URI, preserving any query."""
    from urllib.parse import urlencode, urlparse, urlunparse

    parts = urlparse(redirect_uri)
    query = parts.query
    extra = urlencode({k: v for k, v in params.items() if v is not None})
    combined = f"{query}&{extra}" if query else extra
    return urlunparse(parts._replace(query=combined))
