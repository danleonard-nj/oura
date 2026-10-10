"""Oura webhook verification and handling.

Oura's contract, in order:

* Subscription setup sends a GET carrying `verification_token` and `challenge`.
  We echo the challenge back only if the token matches ours.
* Events arrive as POSTs signed with `x-oura-signature`, an uppercase hex
  HMAC-SHA256 over `x-oura-timestamp` concatenated with the raw body, keyed on
  the application's client secret.
* The endpoint must answer inside 10 seconds, so the work done per event is
  deliberately tiny: one Redis INCR per affected resource plus one Mongo
  insert. There is nothing here that needs a task queue.
"""

from __future__ import annotations

import hashlib
import hmac

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.utilities.logging import get_correlation_id, get_logger
from app.models.audit import AuditAction, AuditEvent
from app.models.resources import WEBHOOK_DATA_TYPE_RESOURCES
from app.services.audit_service import AuditService
from app.services.cache_service import CacheService

from app.models.config import OuraConfig, WebhookConfig
from app.models.webhooks import WebhookEvent, WebhookRecord
from app.repositories.webhook_repository import WebhookRepository

logger = get_logger(__name__)

DEDUPE_PREFIX = "oura:webhook:seen"


class WebhookVerificationError(Exception):
    """The request did not prove it came from Oura."""


class WebhookService:
    def __init__(
        self,
        cache: CacheService,
        repository: WebhookRepository,
        redis: Redis,
        audit: AuditService,
        oura: OuraConfig,
        config: WebhookConfig,
    ) -> None:
        self._cache = cache
        self._repository = repository
        self._redis = redis
        self._audit = audit
        self._client_secret = oura.client_secret.get_secret_value().encode()
        self._verification_token = (
            config.verification_token.get_secret_value()
            if config.verification_token
            else None
        )
        self._account_id = oura.account_id
        self._dedupe_ttl = config.dedupe_ttl_seconds

    # ── Verification ──────────────────────────────────────────────────────

    def verify_challenge(self, verification_token: str | None, challenge: str | None) -> str:
        """Answer Oura's subscription challenge, or refuse."""
        if self._verification_token is None:
            raise WebhookVerificationError(
                "webhooks.verification_token is not configured; cannot verify a subscription."
            )
        if not verification_token or not challenge:
            raise WebhookVerificationError("Missing verification_token or challenge.")
        if not hmac.compare_digest(verification_token, self._verification_token):
            raise WebhookVerificationError("verification_token does not match.")
        return challenge

    def verify_signature(self, signature: str | None, timestamp: str | None, body: bytes) -> None:
        """Check `x-oura-signature` against the raw request body.

        The signed string is the timestamp followed by the body exactly as it
        arrived; re-serialising the parsed JSON would change the bytes and break
        the comparison.
        """
        if not signature or not timestamp:
            raise WebhookVerificationError("Missing x-oura-signature or x-oura-timestamp header.")

        expected = hmac.new(
            self._client_secret, timestamp.encode() + body, hashlib.sha256
        ).hexdigest().upper()

        if not hmac.compare_digest(expected, signature.strip().upper()):
            raise WebhookVerificationError("Signature does not match the request body.")

    # ── Handling ──────────────────────────────────────────────────────────

    async def handle(self, event: WebhookEvent) -> dict[str, object]:
        """Process one verified event. Fast enough to run before the ack."""
        dedupe_key = event.dedupe_key()

        if await self._already_seen(dedupe_key):
            logger.info("duplicate webhook event ignored", extra=event.sanitized())
            return {"status": "duplicate", "invalidated": []}

        invalidated = await self._invalidate(event.data_type)

        record = WebhookRecord(
            dedupe_key=dedupe_key,
            event_type=event.event_type,
            data_type=event.data_type,
            object_id=event.object_id,
            event_time=event.event_time,
            account_id=self._account_id,
            correlation_id=get_correlation_id(),
            invalidated=invalidated,
        )
        try:
            await self._repository.record(record)
        except Exception as exc:  # noqa: BLE001 - never delay the ack over bookkeeping
            logger.warning(
                "webhook persistence failed",
                extra={**event.sanitized(), "error": str(exc)},
            )

        await self._audit.record(
            AuditEvent(
                action=AuditAction.WEBHOOK_RECEIVED,
                account_id=self._account_id,
                correlation_id=get_correlation_id(),
                resource=event.data_type,
                detail={**event.sanitized(), "invalidated": invalidated},
            )
        )
        return {"status": "accepted", "invalidated": invalidated}

    async def record_rejection(self, reason: str) -> None:
        await self._audit.record(
            AuditEvent(
                action=AuditAction.WEBHOOK_REJECTED,
                account_id=self._account_id,
                correlation_id=get_correlation_id(),
                status="rejected",
                error_message=reason,
            )
        )

    # ── Internals ─────────────────────────────────────────────────────────

    async def _already_seen(self, dedupe_key: str) -> bool:
        """Claim the event in Redis. False means this replica owns it.

        If Redis is down we process the event: a duplicated cache invalidation
        is harmless, whereas dropping the event would leave stale data served.
        """
        try:
            claimed = await self._redis.set(
                f"{DEDUPE_PREFIX}:{dedupe_key}", "1", nx=True, ex=self._dedupe_ttl
            )
            return not claimed
        except RedisError as exc:
            logger.warning("webhook dedupe unavailable", extra={"error": str(exc)})
            return False

    async def _invalidate(self, data_type: str) -> list[str]:
        resources = WEBHOOK_DATA_TYPE_RESOURCES.get(data_type)
        if not resources:
            logger.info("webhook data_type has no mapped resource", extra={"data_type": data_type})
            return []

        invalidated: list[str] = []
        for resource in resources:
            if await self._cache.invalidate(self._account_id, resource):
                invalidated.append(resource.value)
        return invalidated
