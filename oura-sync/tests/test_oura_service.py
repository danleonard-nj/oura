"""Oura service: error translation, retry and cache behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.audit import CacheStatus
from app.models.dates import DateRange
from app.models.errors import (
    DateRangeError,
    OuraAuthError,
    OuraNotFoundError,
    OuraPermissionError,
    OuraRateLimitError,
    OuraUpstreamError,
    OuraValidationError,
)
from app.models.resources import Resource
from app.services.audit_service import AuditService
from app.services.cache_service import CacheService
from app.services.oura_service import OuraService
from tests.conftest import (
    FakeAuditRepository,
    FakeRedis,
    http_error,
)


class RecordingClient:
    """Async client stand-in that records what it was asked for."""

    def __init__(self, result=None, errors=None) -> None:
        self.result = result if result is not None else [{"day": "2026-09-01", "score": 80}]
        self.errors = list(errors or [])
        self.calls: list[tuple] = []
        self.tokens: list[str] = []

    async def fetch_range(self, token, resource, start, end):
        self.tokens.append(token)
        self.calls.append((resource, start, end))
        if self.errors:
            raise self.errors.pop(0)
        return self.result

    async def fetch_personal_info(self, token):
        self.tokens.append(token)
        if self.errors:
            raise self.errors.pop(0)
        return {"age": 40, "biological_sex": "male"}


class StubTokenService:
    def __init__(self, tokens=None, scopes=None) -> None:
        self.tokens = list(tokens or ["token-a", "token-a"])
        self.refresh_calls = 0
        # Empty means "unknown", which the service treats as no opinion.
        self.scopes: set[str] = set(scopes or ())

    async def granted_scopes(self, account_id: str) -> set[str]:
        return self.scopes

    async def get_access_token(self, account_id: str) -> str:
        return self.tokens[min(self.refresh_calls, len(self.tokens) - 1)]

    async def refresh(self, account_id: str, *, seen_version=None, rejected_access_token=None):
        self.refresh_calls += 1
        return None


def build_service(config, client, tokens=None, redis=None):
    redis = redis or FakeRedis()
    audit = AuditService(FakeAuditRepository())  # type: ignore[arg-type]
    service = OuraService(
        client=client,  # type: ignore[arg-type]
        token_service=tokens or StubTokenService(),  # type: ignore[arg-type]
        cache=CacheService(redis, config.cache),  # type: ignore[arg-type]
        audit=audit,
        oura=config.oura,
    )
    return service, redis


RANGE = DateRange(start=date(2026, 9, 1), end=date(2026, 9, 3))


# ── Offloading ────────────────────────────────────────────────────────────


async def test_the_client_is_awaited_not_offloaded(config):
    """There is no thread pool any more: the client is awaited directly.

    The old design ran a synchronous `requests` client on a bounded executor.
    This asserts the replacement is genuinely async -- a non-awaitable client
    would raise rather than return records.
    """
    client = RecordingClient()
    service, _ = build_service(config, client)

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert result.records == client.result
    assert client.calls == [(Resource.DAILY_SLEEP, "2026-09-01", "2026-09-03")]


async def test_personal_info_is_also_awaited(config):
    client = RecordingClient()
    service, _ = build_service(config, client)

    fetch = await service.get_personal_info()

    assert fetch.records["age"] == 40


# ── Translation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, OuraValidationError),
        (401, OuraAuthError),
        (403, OuraPermissionError),
        (404, OuraNotFoundError),
        (429, OuraRateLimitError),
        (500, OuraUpstreamError),
        (503, OuraUpstreamError),
    ],
)
async def test_http_status_maps_to_domain_error(config, status, expected):
    # One attempt only, so retryable statuses surface instead of being retried.
    config = config.replace("oura", max_attempts=1)
    client = RecordingClient(errors=[http_error(status)])
    # A 401 would otherwise trigger the refresh path; give it a token service
    # that has already refreshed.
    service, _ = build_service(config, client)

    if status == 401:
        client.errors = [http_error(401), http_error(401)]

    with pytest.raises(expected):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)


async def test_permission_error_names_the_missing_scope(config):
    config = config.replace("oura", max_attempts=1)
    client = RecordingClient(errors=[http_error(403)])
    service, _ = build_service(config, client)

    with pytest.raises(OuraPermissionError) as excinfo:
        await service.get_records(Resource.HEARTRATE, RANGE)

    assert "heartrate" in str(excinfo.value)


async def test_timeouts_and_connection_failures_become_upstream_errors(config):
    import requests

    config = config.replace("oura", max_attempts=1)
    for exc in (requests.Timeout("slow"), requests.ConnectionError("no route")):
        client = RecordingClient(errors=[exc])
        service, _ = build_service(config, client)
        with pytest.raises(OuraUpstreamError):
            await service.get_records(Resource.DAILY_SLEEP, RANGE)


async def test_error_messages_never_contain_token_material(config):
    """A translated error is shown to the model; it must be credential-free."""
    config = config.replace("oura", max_attempts=1)
    client = RecordingClient(errors=[http_error(403)])
    service, _ = build_service(config, client, tokens=StubTokenService(["super-secret-token"]))

    with pytest.raises(OuraPermissionError) as excinfo:
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert "super-secret-token" not in str(excinfo.value)
    assert "Bearer" not in str(excinfo.value)


# ── Retry ─────────────────────────────────────────────────────────────────


async def test_401_refreshes_then_retries_exactly_once(config):
    client = RecordingClient(errors=[http_error(401)])
    tokens = StubTokenService(["expired-token", "fresh-token"])
    service, _ = build_service(config, client, tokens=tokens)

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert tokens.refresh_calls == 1
    assert len(client.calls) == 2
    assert client.tokens == ["expired-token", "fresh-token"]
    assert result.records == client.result


async def test_resilience_401_recovers_with_real_token_service(config):
    from tests.test_token_service import build_service as build_tokens
    from tests.conftest import FakeOAuthClient, make_token

    oauth = FakeOAuthClient()
    tokens, _ = build_tokens(config, token=make_token(access="rejected"), oauth=oauth)
    client = RecordingClient(errors=[http_error(401)])
    service, _ = build_service(config, client, tokens=tokens)

    await service.get_records(Resource.DAILY_RESILIENCE, RANGE)

    assert len(oauth.refresh_calls) == 1
    assert client.tokens == ["rejected", "access-1"]


async def test_repeated_401_is_not_retried_forever(config):
    client = RecordingClient(errors=[http_error(401), http_error(401), http_error(401)])
    tokens = StubTokenService(["a", "b"])
    service, _ = build_service(config, client, tokens=tokens)

    with pytest.raises(OuraAuthError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert tokens.refresh_calls == 1
    assert len(client.calls) == 2


async def test_failed_request_logs_actual_upstream_status(config, caplog):
    import logging

    config = config.replace("oura", max_attempts=1)
    service, _ = build_service(config, RecordingClient(errors=[http_error(503)]))
    with caplog.at_level(logging.WARNING), pytest.raises(OuraUpstreamError):
        await service.get_records(Resource.DAILY_RESILIENCE, RANGE)

    record = next(r for r in caplog.records if r.getMessage() == "oura_request error")
    assert record.upstream_status == 503
    assert record.levelno == logging.WARNING
    assert "503" in record.error_message
    assert not hasattr(record, "tool")


async def test_transient_5xx_is_retried_and_can_succeed(config):
    client = RecordingClient(errors=[http_error(503)])
    service, _ = build_service(config, client)

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 2
    assert result.records == client.result


async def test_retries_are_bounded(config):
    config = config.replace("oura", max_attempts=2)
    client = RecordingClient(errors=[http_error(500)] * 5)
    service, _ = build_service(config, client)

    with pytest.raises(OuraUpstreamError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 2, "retry budget was not respected"


async def test_validation_errors_are_not_retried(config):
    client = RecordingClient(errors=[http_error(400)] * 3)
    service, _ = build_service(config, client)

    with pytest.raises(OuraValidationError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 1


async def test_rate_limit_backoff_honours_retry_after(config):
    client = RecordingClient(errors=[http_error(429, {"Retry-After": "7"})])
    service, _ = build_service(config, client)

    error = service._translate(http_error(429, {"Retry-After": "7"}), Resource.DAILY_SLEEP)
    assert isinstance(error, OuraRateLimitError)
    assert error.retry_after == 7.0
    assert service._backoff(error, attempt=1) == 7.0


async def test_unparseable_retry_after_falls_back_to_backoff(config):
    service, _ = build_service(config, RecordingClient())
    error = service._translate(
        http_error(429, {"Retry-After": "in a bit"}), Resource.DAILY_SLEEP
    )
    assert error.retry_after is None
    assert 0 < service._backoff(error, attempt=1) <= 1.0


# ── Cache ─────────────────────────────────────────────────────────────────


async def test_second_identical_request_is_a_cache_hit(config):
    client = RecordingClient()
    service, _ = build_service(config, client)

    first = await service.get_records(Resource.DAILY_SLEEP, RANGE)
    second = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert first.cache is CacheStatus.MISS
    assert second.cache is CacheStatus.HIT
    assert len(client.calls) == 1, "a cache hit still called Oura"
    assert second.records == first.records


async def test_a_different_date_range_is_a_separate_cache_entry(config):
    client = RecordingClient()
    service, _ = build_service(config, client)

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    await service.get_records(
        Resource.DAILY_SLEEP, DateRange(start=date(2026, 8, 1), end=date(2026, 8, 3))
    )

    assert len(client.calls) == 2


async def test_cache_can_be_bypassed(config):
    client = RecordingClient()
    service, _ = build_service(config, client)

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    result = await service.get_records(Resource.DAILY_SLEEP, RANGE, use_cache=False)

    assert result.cache is CacheStatus.BYPASS
    assert len(client.calls) == 2


async def test_invalidation_forces_a_refetch(config):
    client = RecordingClient()
    service, redis = build_service(config, client)
    cache = CacheService(redis, config.cache)  # type: ignore[arg-type]

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    await cache.invalidate("default", Resource.DAILY_SLEEP)
    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert result.cache is CacheStatus.MISS
    assert len(client.calls) == 2


async def test_redis_outage_degrades_to_a_live_fetch(config):
    client = RecordingClient()
    service, _ = build_service(config, client, redis=FakeRedis(available=False))

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert result.cache is CacheStatus.UNAVAILABLE
    assert result.records == client.result


# ── Range validation ──────────────────────────────────────────────────────


async def test_heart_rate_range_is_capped_tightly(config):
    service, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError) as excinfo:
        service.resolve_range(Resource.HEARTRATE, "2026-01-01", "2026-06-01")

    assert "14-day maximum" in str(excinfo.value)


async def test_absurd_ranges_are_refused(config):
    service, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "1990-01-01", "2026-01-01")


async def test_inverted_range_is_refused(config):
    service, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "2026-09-10", "2026-09-01")


async def test_malformed_date_is_refused(config):
    service, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "last tuesday", None)


# ── Scope preflight ───────────────────────────────────────────────────────
#
# Oura answers 401 for a missing scope, so an ungranted collection costs a
# round trip and looks like an auth failure. When the stored token tells us the
# scope was never granted, refuse locally instead.


async def test_an_ungranted_scope_is_refused_without_calling_oura(config):
    client = RecordingClient()
    tokens = StubTokenService(scopes={"daily", "personal"})
    service, _ = build_service(config, client, tokens=tokens)

    with pytest.raises(OuraPermissionError) as excinfo:
        await service.get_records(Resource.DAILY_RESILIENCE, RANGE)

    assert client.calls == [], "a request went out for a scope we know is missing"
    assert "stress" in str(excinfo.value)


async def test_a_granted_scope_still_reaches_oura(config):
    client = RecordingClient()
    tokens = StubTokenService(scopes={"daily", "stress"})
    service, _ = build_service(config, client, tokens=tokens)

    await service.get_records(Resource.DAILY_RESILIENCE, RANGE)

    assert len(client.calls) == 1


async def test_unknown_scopes_do_not_block_anything(config):
    """An older token record has no scopes; Oura decides, not us."""
    client = RecordingClient()
    tokens = StubTokenService(scopes=set())
    service, _ = build_service(config, client, tokens=tokens)

    await service.get_records(Resource.DAILY_RESILIENCE, RANGE)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("resource", "scope"),
    [
        (Resource.DAILY_RESILIENCE, "stress"),
        (Resource.DAILY_CARDIOVASCULAR_AGE, "heart_health"),
        (Resource.VO2_MAX, "heart_health"),
        (Resource.RING_CONFIGURATION, "ring_configuration"),
        (Resource.RING_BATTERY_LEVEL, "ring_configuration"),
        (Resource.DAILY_SPO2, "spo2"),
        (Resource.HEARTRATE, "heartrate"),
        (Resource.WORKOUT, "workout"),
        (Resource.SESSION, "session"),
        (Resource.ENHANCED_TAG, "tag"),
        (Resource.PERSONAL_INFO, "personal"),
    ],
)
def test_each_resource_declares_the_scope_oura_actually_demands(resource, scope):
    """Verified by probing every endpoint with a live token.

    Three of these -- stress, heart_health, ring_configuration -- are absent
    from Oura's OpenAPI spec entirely.
    """
    from app.models.resources import spec_for

    assert spec_for(resource).scope == scope


async def test_the_extapi_prefix_oura_returns_is_stripped(config):
    """Oura echoes scopes back as `extapi:daily`; the stored form must match."""
    from app.services.token_service import TokenService
    from tests.conftest import FakeAuditRepository, FakeOAuthClient, FakeTokenRepository, make_token

    repository = FakeTokenRepository(
        make_token(scopes=["extapi:daily", "extapi:stress"])
    )
    tokens = TokenService(
        repository=repository,  # type: ignore[arg-type]
        oauth_client=FakeOAuthClient(),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        audit=AuditService(FakeAuditRepository()),  # type: ignore[arg-type]
        config=config.tokens,
    )

    assert await tokens.granted_scopes("default") == {"daily", "stress"}
