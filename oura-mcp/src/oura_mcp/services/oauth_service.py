"""Server-side Authorization Code flow orchestration."""

from __future__ import annotations

import secrets

from redis.asyncio import Redis
from redis.exceptions import RedisError

from oura_mcp.clients.oauth_client import OuraOAuthClient
from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import get_correlation_id, get_logger
from oura_mcp.models.audit import AuditAction, AuditEvent
from oura_mcp.models.tokens import ConnectionStatus
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.token_service import TokenService

logger = get_logger(__name__)

STATE_PREFIX = "oura:oauth:state"


class OAuthStateError(Exception):
    """The `state` parameter on the callback was missing, unknown or reused."""


class OAuthService:
    """Drives the browser round trip and hands the resulting grant to TokenService.

    The `state` nonce lives in Redis so the callback can land on any replica,
    and is consumed atomically so a replayed callback is rejected.
    """

    def __init__(
        self,
        oauth_client: OuraOAuthClient,
        token_service: TokenService,
        redis: Redis,
        audit: AuditService,
        config: Config,
    ) -> None:
        self._oauth = oauth_client
        self._tokens = token_service
        self._redis = redis
        self._audit = audit
        self._account_id = config.oura_account_id
        self._state_ttl = config.oauth_state_ttl_seconds

    async def start(self) -> str:
        """Mint a state nonce and return the Oura authorization URL."""
        state = secrets.token_urlsafe(32)
        try:
            await self._redis.set(f"{STATE_PREFIX}:{state}", self._account_id, ex=self._state_ttl)
        except RedisError as exc:
            raise OAuthStateError(
                "Cannot start the OAuth flow: Redis is unavailable, so the state nonce "
                "cannot be stored and the callback could not be validated."
            ) from exc

        await self._audit.record(
            AuditEvent(
                action=AuditAction.OAUTH_AUTHORIZE,
                account_id=self._account_id,
                correlation_id=get_correlation_id(),
            )
        )
        return self._oauth.authorization_url(state)

    async def complete(self, code: str, state: str) -> ConnectionStatus:
        """Validate the callback, exchange the code and persist the grant."""
        account_id = await self._consume_state(state)

        grant = await self._oauth.exchange_code(code)
        token = await self._tokens.store_grant(account_id, grant)

        await self._audit.record(
            AuditEvent(
                action=AuditAction.OAUTH_CALLBACK,
                account_id=account_id,
                correlation_id=get_correlation_id(),
                detail={"scopes": token.scopes},
            )
        )
        logger.info(
            "oura account connected",
            extra={"account_id": account_id, "scopes": token.scopes},
        )
        return await self._tokens.status(account_id)

    async def _consume_state(self, state: str) -> str:
        if not state:
            raise OAuthStateError("The callback did not include a state parameter.")
        try:
            account_id = await self._redis.getdel(f"{STATE_PREFIX}:{state}")
        except RedisError as exc:
            raise OAuthStateError(
                "Cannot complete the OAuth flow: Redis is unavailable, so the state "
                "nonce cannot be validated."
            ) from exc

        if account_id is None:
            raise OAuthStateError(
                "Unrecognised or expired state parameter. Start the flow again at /oauth/authorize."
            )
        return account_id
