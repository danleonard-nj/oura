"""OAuth token lifecycle, safe across replicas.

Oura refresh tokens rotate and are single-use. If two replicas refresh with the
same stored token, one of them wins and the other's response is worthless --
worse, the account can end up with no usable refresh token at all. The sequence
below is what makes that impossible:

1.  Take a Redis lock scoped to the account.
2.  Re-read the persisted token, because another replica may have refreshed
    while we waited.
3.  If it is now valid and was not rejected by Oura, use it and refresh nothing.
4.  Otherwise call Oura, and persist the new access *and* refresh token in a
    single atomic update before releasing the lock.

If Redis is unavailable the refresh is refused rather than attempted. A failed
request is recoverable; a burned refresh token is not.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError

from app.clients.oauth_client import OuraOAuthClient
from app.models.config import TokenConfig
from app.utilities.logging import get_correlation_id, get_logger
from app.models.audit import AuditAction, AuditEvent
from app.models.errors import (
    OuraNotConnectedError,
    TokenRefreshError,
    TokenRefreshUnavailableError,
)
from app.models.tokens import ConnectionStatus, OuraToken, TokenGrant
from app.repositories.token_repository import TokenRepository
from app.services.audit_service import AuditService

logger = get_logger(__name__)

LOCK_PREFIX = "oura:lock:refresh"


class TokenService:
    def __init__(
        self,
        repository: TokenRepository,
        oauth_client: OuraOAuthClient,
        redis: Redis,
        audit: AuditService,
        config: TokenConfig,
    ) -> None:
        self._repository = repository
        self._oauth = oauth_client
        self._redis = redis
        self._audit = audit
        self._skew = config.expiry_skew_seconds
        # The lock TTL is the ceiling on how long a crashed or wedged replica
        # can hold up everyone else. Redis expires it regardless of what
        # happened to the holder, so a stuck refresh cannot become permanent.
        self._lock_ttl = config.refresh_lock_ttl_seconds
        self._lock_wait = config.refresh_lock_wait_seconds

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get_access_token(self, account_id: str) -> str:
        """Return a usable access token, refreshing first if it is close to expiry."""
        token = await self._require_token(account_id)
        if not token.is_expired(skew_seconds=self._skew):
            return token.access_token
        refreshed = await self.refresh(account_id, seen_version=token.version)
        return refreshed.access_token

    async def status(self, account_id: str) -> ConnectionStatus:
        return await self._repository.status(account_id)

    async def granted_scopes(self, account_id: str) -> set[str]:
        """Scopes Oura actually granted, normalised.

        Oura echoes them back prefixed -- `extapi:daily` rather than `daily` --
        so the prefix is stripped to match what we requested. An empty set
        means unknown, and callers must treat that as "do not block".
        """
        token = await self._repository.get(account_id)
        if token is None:
            return set()
        return {scope.removeprefix("extapi:") for scope in token.scopes}

    # ── Writes ────────────────────────────────────────────────────────────

    async def store_grant(self, account_id: str, grant: TokenGrant) -> OuraToken:
        """Persist a freshly issued grant from the authorization-code exchange."""
        stored = await self._repository.save(grant.to_token(account_id))
        await self._audit.record(
            AuditEvent(
                action=AuditAction.TOKEN_STORED,
                account_id=account_id,
                correlation_id=get_correlation_id(),
                detail={"scopes": stored.scopes, "version": stored.version},
            )
        )
        return stored

    async def refresh(
        self, account_id: str, *, seen_version: int | None = None,
        rejected_access_token: str | None = None,
    ) -> OuraToken:
        """Rotate the token pair under a distributed lock.

        `seen_version` is the version the caller was working from. If the stored
        version has moved on, another replica already rotated and that result is
        returned instead of burning another refresh token.

        `rejected_access_token` forces rotation of that specific token after a
        401, even if its recorded expiry is in the future. A different valid
        token found under the lock is reused; concurrent 401s cannot rotate it
        repeatedly. The rejected credential must never be logged.
        """
        try:
            lock = self._redis.lock(
                f"{LOCK_PREFIX}:{account_id}",
                timeout=self._lock_ttl,
                blocking=True,
                blocking_timeout=self._lock_wait,
                thread_local=False,
            )
            acquired = await lock.acquire()
        except RedisError as exc:
            # Refusing is the safe outcome: without the lock we cannot rule out
            # a concurrent replica using the same single-use refresh token.
            raise TokenRefreshUnavailableError(
                "Cannot refresh the Oura token: the coordination lock is unavailable. "
                "Refusing rather than risk two replicas consuming the same single-use "
                "refresh token."
            ) from exc

        if not acquired:
            raise TokenRefreshUnavailableError(
                f"Timed out after {self._lock_wait}s waiting for another replica to finish "
                "refreshing the Oura token."
            )

        try:
            return await self._refresh_locked(account_id, seen_version, rejected_access_token)
        finally:
            try:
                await lock.release()
            except LockError:
                # The TTL expired while we worked; another replica may already
                # hold it. Nothing to release and nothing to fix.
                logger.warning(
                    "refresh lock had already expired on release",
                    extra={"account_id": account_id},
                )

    async def _refresh_locked(
        self, account_id: str, seen_version: int | None,
        rejected_access_token: str | None = None,
    ) -> OuraToken:
        # Step 2: re-read under the lock. Another replica may have done the work
        # while we were queued behind it.
        current = await self._require_token(account_id)

        if rejected_access_token is not None:
            if current.access_token != rejected_access_token and not current.is_expired(skew_seconds=self._skew):
                return current
        elif seen_version is not None and current.version != seen_version:
            if not current.is_expired(skew_seconds=self._skew):
                logger.info(
                    "token already refreshed by another replica",
                    extra={"account_id": account_id, "version": current.version},
                )
                return current
        elif not current.is_expired(skew_seconds=self._skew):
            return current

        grant = await self._oauth.refresh(current.refresh_token)

        # Step 5: both halves land in one atomic update. A reader can never see
        # the new access token paired with the dead refresh token.
        stored = await self._repository.save(
            grant.to_token(account_id, version=current.version)
        )

        await self._audit.record(
            AuditEvent(
                action=AuditAction.TOKEN_REFRESH,
                account_id=account_id,
                correlation_id=get_correlation_id(),
                detail={"version": stored.version, "scopes": stored.scopes},
            )
        )
        logger.info(
            "oura token refreshed",
            extra={"account_id": account_id, "version": stored.version},
        )
        return stored

    async def disconnect(self, account_id: str) -> bool:
        return await self._repository.delete(account_id)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _require_token(self, account_id: str) -> OuraToken:
        token = await self._repository.get(account_id)
        if token is None:
            raise OuraNotConnectedError(
                f"No Oura account is connected for '{account_id}'. "
                "Complete the OAuth flow at /oauth/authorize, or run the oura-auth bootstrap."
            )
        return token


__all__ = ["TokenService", "TokenRefreshError", "TokenRefreshUnavailableError"]
