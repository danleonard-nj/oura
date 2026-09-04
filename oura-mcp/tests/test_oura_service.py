"""Oura service: threading, error translation, retry and cache behaviour."""

from __future__ import annotations

import threading
from datetime import date

import pytest

from oura_mcp.models.audit import CacheStatus
from oura_mcp.models.dates import DateRange
from oura_mcp.models.errors import (
    DateRangeError,
    OuraAuthError,
    OuraNotFoundError,
    OuraPermissionError,
    OuraRateLimitError,
    OuraUpstreamError,
    OuraValidationError,
)
from oura_mcp.models.resources import Resource
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.cache_service import CacheService
from oura_mcp.services.oura_service import OuraService
from tests.conftest import (
    MAIN_THREAD,
    FakeAuditRepository,
    FakeOffloader,
    FakeRedis,
    http_error,
)


class RecordingClient:
    """Blocking client stand-in that records the thread it ran on."""

    def __init__(self, result=None, errors=None) -> None:
        self.result = result if result is not None else [{"day": "2026-09-01", "score": 80}]
        self.errors = list(errors or [])
        self.calls: list[tuple] = []
        self.threads: list[int] = []
        self.tokens: list[str] = []

    def fetch_range(self, token, resource, start, end):
        self.threads.append(threading.get_ident())
        self.tokens.append(token)
        self.calls.append((resource, start, end))
        if self.errors:
            raise self.errors.pop(0)
        return self.result

    def fetch_personal_info(self, token):
        self.threads.append(threading.get_ident())
        self.tokens.append(token)
        if self.errors:
            raise self.errors.pop(0)
        return {"age": 40, "biological_sex": "male"}


class StubTokenService:
    def __init__(self, tokens=None) -> None:
        self.tokens = list(tokens or ["token-a", "token-a"])
        self.refresh_calls = 0

    async def get_access_token(self, account_id: str) -> str:
        return self.tokens[min(self.refresh_calls, len(self.tokens) - 1)]

    async def refresh(self, account_id: str, *, seen_version=None):
        self.refresh_calls += 1
        return None


def build_service(config, client, tokens=None, redis=None):
    redis = redis or FakeRedis()
    audit = AuditService(FakeAuditRepository())  # type: ignore[arg-type]
    offloader = FakeOffloader()
    service = OuraService(
        client=client,  # type: ignore[arg-type]
        token_service=tokens or StubTokenService(),  # type: ignore[arg-type]
        cache=CacheService(redis, config),  # type: ignore[arg-type]
        offloader=offloader,  # type: ignore[arg-type]
        audit=audit,
        config=config,
    )
    return service, offloader, redis


RANGE = DateRange(start=date(2026, 9, 1), end=date(2026, 9, 3))


# ── Threading ─────────────────────────────────────────────────────────────


async def test_blocking_client_never_runs_on_the_event_loop(config):
    """The whole reason the offloader exists."""
    client = RecordingClient()
    service, offloader, _ = build_service(config, client)

    await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert client.threads, "the client was never called"
    assert MAIN_THREAD not in client.threads, "a blocking Oura call ran on the event loop thread"
    assert offloader.thread_ids == client.threads


async def test_personal_info_is_also_offloaded(config):
    client = RecordingClient()
    service, _, _ = build_service(config, client)

    await service.get_personal_info()

    assert MAIN_THREAD not in client.threads


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
    config = config.model_copy(update={"oura_max_attempts": 1})
    client = RecordingClient(errors=[http_error(status)])
    # A 401 would otherwise trigger the refresh path; give it a token service
    # that has already refreshed.
    service, _, _ = build_service(config, client)

    if status == 401:
        client.errors = [http_error(401), http_error(401)]

    with pytest.raises(expected):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)


async def test_permission_error_names_the_missing_scope(config):
    config = config.model_copy(update={"oura_max_attempts": 1})
    client = RecordingClient(errors=[http_error(403)])
    service, _, _ = build_service(config, client)

    with pytest.raises(OuraPermissionError) as excinfo:
        await service.get_records(Resource.HEARTRATE, RANGE)

    assert "heartrate" in str(excinfo.value)


async def test_timeouts_and_connection_failures_become_upstream_errors(config):
    import requests

    config = config.model_copy(update={"oura_max_attempts": 1})
    for exc in (requests.Timeout("slow"), requests.ConnectionError("no route")):
        client = RecordingClient(errors=[exc])
        service, _, _ = build_service(config, client)
        with pytest.raises(OuraUpstreamError):
            await service.get_records(Resource.DAILY_SLEEP, RANGE)


async def test_error_messages_never_contain_token_material(config):
    """A translated error is shown to the model; it must be credential-free."""
    config = config.model_copy(update={"oura_max_attempts": 1})
    client = RecordingClient(errors=[http_error(403)])
    service, _, _ = build_service(config, client, tokens=StubTokenService(["super-secret-token"]))

    with pytest.raises(OuraPermissionError) as excinfo:
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert "super-secret-token" not in str(excinfo.value)
    assert "Bearer" not in str(excinfo.value)


# ── Retry ─────────────────────────────────────────────────────────────────


async def test_401_refreshes_then_retries_exactly_once(config):
    client = RecordingClient(errors=[http_error(401)])
    tokens = StubTokenService(["expired-token", "fresh-token"])
    service, _, _ = build_service(config, client, tokens=tokens)

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert tokens.refresh_calls == 1
    assert len(client.calls) == 2
    assert client.tokens == ["expired-token", "fresh-token"]
    assert result.records == client.result


async def test_repeated_401_is_not_retried_forever(config):
    client = RecordingClient(errors=[http_error(401), http_error(401), http_error(401)])
    tokens = StubTokenService(["a", "b"])
    service, _, _ = build_service(config, client, tokens=tokens)

    with pytest.raises(OuraAuthError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert tokens.refresh_calls == 1
    assert len(client.calls) == 2


async def test_transient_5xx_is_retried_and_can_succeed(config):
    client = RecordingClient(errors=[http_error(503)])
    service, _, _ = build_service(config, client)

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 2
    assert result.records == client.result


async def test_retries_are_bounded(config):
    config = config.model_copy(update={"oura_max_attempts": 2})
    client = RecordingClient(errors=[http_error(500)] * 5)
    service, _, _ = build_service(config, client)

    with pytest.raises(OuraUpstreamError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 2, "retry budget was not respected"


async def test_validation_errors_are_not_retried(config):
    client = RecordingClient(errors=[http_error(400)] * 3)
    service, _, _ = build_service(config, client)

    with pytest.raises(OuraValidationError):
        await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert len(client.calls) == 1


async def test_rate_limit_backoff_honours_retry_after(config):
    client = RecordingClient(errors=[http_error(429, {"Retry-After": "7"})])
    service, _, _ = build_service(config, client)

    error = service._translate(http_error(429, {"Retry-After": "7"}), Resource.DAILY_SLEEP)
    assert isinstance(error, OuraRateLimitError)
    assert error.retry_after == 7.0
    assert service._backoff(error, attempt=1) == 7.0


async def test_unparseable_retry_after_falls_back_to_backoff(config):
    service, _, _ = build_service(config, RecordingClient())
    error = service._translate(
        http_error(429, {"Retry-After": "in a bit"}), Resource.DAILY_SLEEP
    )
    assert error.retry_after is None
    assert 0 < service._backoff(error, attempt=1) <= 1.0


# ── Cache ─────────────────────────────────────────────────────────────────


async def test_second_identical_request_is_a_cache_hit(config):
    client = RecordingClient()
    service, _, _ = build_service(config, client)

    first = await service.get_records(Resource.DAILY_SLEEP, RANGE)
    second = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert first.cache is CacheStatus.MISS
    assert second.cache is CacheStatus.HIT
    assert len(client.calls) == 1, "a cache hit still called Oura"
    assert second.records == first.records


async def test_a_different_date_range_is_a_separate_cache_entry(config):
    client = RecordingClient()
    service, _, _ = build_service(config, client)

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    await service.get_records(
        Resource.DAILY_SLEEP, DateRange(start=date(2026, 8, 1), end=date(2026, 8, 3))
    )

    assert len(client.calls) == 2


async def test_cache_can_be_bypassed(config):
    client = RecordingClient()
    service, _, _ = build_service(config, client)

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    result = await service.get_records(Resource.DAILY_SLEEP, RANGE, use_cache=False)

    assert result.cache is CacheStatus.BYPASS
    assert len(client.calls) == 2


async def test_invalidation_forces_a_refetch(config):
    client = RecordingClient()
    service, _, redis = build_service(config, client)
    cache = CacheService(redis, config)  # type: ignore[arg-type]

    await service.get_records(Resource.DAILY_SLEEP, RANGE)
    await cache.invalidate("default", Resource.DAILY_SLEEP)
    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert result.cache is CacheStatus.MISS
    assert len(client.calls) == 2


async def test_redis_outage_degrades_to_a_live_fetch(config):
    client = RecordingClient()
    service, _, _ = build_service(config, client, redis=FakeRedis(available=False))

    result = await service.get_records(Resource.DAILY_SLEEP, RANGE)

    assert result.cache is CacheStatus.UNAVAILABLE
    assert result.records == client.result


# ── Range validation ──────────────────────────────────────────────────────


async def test_heart_rate_range_is_capped_tightly(config):
    service, _, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError) as excinfo:
        service.resolve_range(Resource.HEARTRATE, "2026-01-01", "2026-06-01")

    assert "14-day maximum" in str(excinfo.value)


async def test_absurd_ranges_are_refused(config):
    service, _, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "1990-01-01", "2026-01-01")


async def test_inverted_range_is_refused(config):
    service, _, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "2026-09-10", "2026-09-01")


async def test_malformed_date_is_refused(config):
    service, _, _ = build_service(config, RecordingClient())

    with pytest.raises(DateRangeError):
        service.resolve_range(Resource.DAILY_SLEEP, "last tuesday", None)
