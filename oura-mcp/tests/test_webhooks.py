"""Webhook signature verification, challenge handling and event processing."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from oura_mcp.models.resources import Resource
from oura_mcp.models.webhooks import WebhookEvent
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.cache_service import CacheService
from oura_mcp.services.webhook_service import WebhookService, WebhookVerificationError
from tests.conftest import (
    FakeAuditRepository,
    FakeRedis,
    FakeWebhookRepository,
)


def build_service(config, redis=None, repository=None):
    redis = redis or FakeRedis()
    repository = repository or FakeWebhookRepository()
    return (
        WebhookService(
            cache=CacheService(redis, config),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
            redis=redis,  # type: ignore[arg-type]
            audit=AuditService(FakeAuditRepository()),  # type: ignore[arg-type]
            config=config,
        ),
        redis,
        repository,
    )


def sign(config, timestamp: str, body: bytes) -> str:
    """Reproduce Oura's signature: uppercase hex HMAC-SHA256 of timestamp + body."""
    return hmac.new(
        config.oura_client_secret.get_secret_value().encode(),
        timestamp.encode() + body,
        hashlib.sha256,
    ).hexdigest().upper()


BODY = json.dumps(
    {
        "event_type": "update",
        "data_type": "daily_sleep",
        "object_id": "abc123",
        "event_time": "2026-09-01T08:00:00+00:00",
        "user_id": "user-1",
    },
    separators=(",", ":"),
).encode()


# ── Challenge ─────────────────────────────────────────────────────────────


def test_challenge_is_echoed_for_a_matching_token(config):
    service, _, _ = build_service(config)

    assert service.verify_challenge("test-verification-token", "nonce-42") == "nonce-42"


def test_challenge_is_refused_for_a_wrong_token(config):
    service, _, _ = build_service(config)

    with pytest.raises(WebhookVerificationError):
        service.verify_challenge("not-the-token", "nonce-42")


def test_challenge_is_refused_when_no_token_is_configured(config):
    service, _, _ = build_service(
        config.model_copy(update={"oura_webhook_verification_token": None})
    )

    with pytest.raises(WebhookVerificationError):
        service.verify_challenge("anything", "nonce")


# ── Signature ─────────────────────────────────────────────────────────────


def test_valid_signature_is_accepted(config):
    service, _, _ = build_service(config)
    timestamp = "1767225600"

    service.verify_signature(sign(config, timestamp, BODY), timestamp, BODY)


def test_lowercase_signature_is_accepted(config):
    """Oura documents uppercase hex; accept either casing of the same digest."""
    service, _, _ = build_service(config)
    timestamp = "1767225600"

    service.verify_signature(sign(config, timestamp, BODY).lower(), timestamp, BODY)


def test_tampered_body_is_rejected(config):
    service, _, _ = build_service(config)
    timestamp = "1767225600"
    signature = sign(config, timestamp, BODY)

    with pytest.raises(WebhookVerificationError):
        service.verify_signature(signature, timestamp, BODY.replace(b"abc123", b"evil99"))


def test_mismatched_timestamp_is_rejected(config):
    """The timestamp is part of the signed string, so swapping it must fail."""
    service, _, _ = build_service(config)
    signature = sign(config, "1767225600", BODY)

    with pytest.raises(WebhookVerificationError):
        service.verify_signature(signature, "1767229999", BODY)


def test_signature_from_a_different_secret_is_rejected(config):
    service, _, _ = build_service(config)
    timestamp = "1767225600"
    forged = hmac.new(b"attacker-secret", timestamp.encode() + BODY, hashlib.sha256)

    with pytest.raises(WebhookVerificationError):
        service.verify_signature(forged.hexdigest().upper(), timestamp, BODY)


@pytest.mark.parametrize(
    ("signature", "timestamp"),
    [(None, "1767225600"), ("ABC", None), (None, None), ("", "")],
)
def test_missing_signature_headers_are_rejected(config, signature, timestamp):
    service, _, _ = build_service(config)

    with pytest.raises(WebhookVerificationError):
        service.verify_signature(signature, timestamp, BODY)


# ── Handling ──────────────────────────────────────────────────────────────


async def test_event_invalidates_the_matching_cache_generations(config):
    service, redis, _ = build_service(config)
    cache = CacheService(redis, config)  # type: ignore[arg-type]
    await cache.set("default", Resource.DAILY_SLEEP, {"a": 1}, ["cached"])

    event = WebhookEvent.model_validate_json(BODY)
    result = await service.handle(event)

    assert result["status"] == "accepted"
    assert "daily_sleep" in result["invalidated"]
    value, _ = await cache.get("default", Resource.DAILY_SLEEP, {"a": 1})
    assert value is None, "the cached entry survived invalidation"


async def test_sleep_event_invalidates_both_sleep_collections(config):
    """A raw sleep change also makes the derived daily summary stale."""
    service, _, _ = build_service(config)
    event = WebhookEvent(event_type="update", data_type="sleep", object_id="x")

    result = await service.handle(event)

    assert set(result["invalidated"]) == {"sleep", "daily_sleep"}


async def test_duplicate_delivery_is_ignored(config):
    service, _, repository = build_service(config)
    event = WebhookEvent.model_validate_json(BODY)

    first = await service.handle(event)
    second = await service.handle(event)

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert len(repository.records) == 1


async def test_unknown_data_type_is_accepted_without_invalidating(config):
    service, _, _ = build_service(config)
    event = WebhookEvent(event_type="create", data_type="something_new", object_id="x")

    result = await service.handle(event)

    assert result["status"] == "accepted"
    assert result["invalidated"] == []


async def test_event_is_processed_even_if_redis_dedupe_is_down(config):
    """Losing dedupe is acceptable; dropping the event and serving stale data is not."""
    service, _, repository = build_service(config, redis=FakeRedis(available=False))
    event = WebhookEvent.model_validate_json(BODY)

    result = await service.handle(event)

    assert result["status"] == "accepted"
    assert len(repository.records) == 1


async def test_persistence_failure_does_not_fail_the_acknowledgement(config):
    class BrokenRepository(FakeWebhookRepository):
        async def record(self, event):
            raise RuntimeError("mongo is down")

    service, _, _ = build_service(config, repository=BrokenRepository())
    event = WebhookEvent.model_validate_json(BODY)

    result = await service.handle(event)

    assert result["status"] == "accepted"


def test_sanitized_metadata_excludes_the_user_id(config):
    event = WebhookEvent.model_validate_json(BODY)

    sanitized = event.sanitized()

    assert "user_id" not in sanitized
    assert sanitized["has_user_id"] is True
    assert sanitized["data_type"] == "daily_sleep"
