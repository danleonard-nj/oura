"""Token refresh, rotation and the concurrency guarantees around them."""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import RedisError

from app.models.errors import OuraNotConnectedError, TokenRefreshUnavailableError
from app.services.token_service import TokenService
from tests.conftest import (
    FakeOAuthClient,
    FakeRedis,
    FakeTokenRepository,
    make_token,
)


def build_service(config, *, token=None, redis=None, oauth=None, audit_service=None):
    from app.services.audit_service import AuditService
    from tests.conftest import FakeAuditRepository

    repository = FakeTokenRepository(token)
    return (
        TokenService(
            repository=repository,  # type: ignore[arg-type]
            oauth_client=oauth or FakeOAuthClient(),  # type: ignore[arg-type]
            redis=redis or FakeRedis(),  # type: ignore[arg-type]
            audit=audit_service or AuditService(FakeAuditRepository()),  # type: ignore[arg-type]
            config=config.tokens,
        ),
        repository,
    )


async def test_valid_token_is_returned_without_refreshing(config):
    oauth = FakeOAuthClient()
    service, _ = build_service(config, token=make_token(access="live"), oauth=oauth)

    assert await service.get_access_token("default") == "live"
    assert oauth.refresh_calls == []


async def test_rejected_unexpired_token_is_refreshed(config):
    oauth = FakeOAuthClient()
    service, repository = build_service(config, token=make_token(access="rejected"), oauth=oauth)

    result = await service.refresh("default", rejected_access_token="rejected")

    assert len(oauth.refresh_calls) == 1
    assert result.access_token == "access-1"
    assert repository.token.refresh_token == "refresh-1"


async def test_concurrent_rejections_refresh_only_the_rejected_token(config):
    oauth = FakeOAuthClient()
    service, repository = build_service(config, token=make_token(access="rejected"), oauth=oauth)

    results = await asyncio.gather(*(
        service.refresh("default", rejected_access_token="rejected") for _ in range(10)
    ))

    assert len(oauth.refresh_calls) == 1
    assert len(repository.saves) == 1
    assert {result.access_token for result in results} == {"access-1"}


async def test_expired_token_triggers_refresh(config):
    oauth = FakeOAuthClient()
    service, repository = build_service(
        config, token=make_token(expires_in=-60, refresh="stale"), oauth=oauth
    )

    assert await service.get_access_token("default") == "access-1"
    assert oauth.refresh_calls == ["stale"]
    assert repository.token is not None
    assert repository.token.refresh_token == "refresh-1"


async def test_token_within_expiry_skew_is_refreshed_early(config):
    """A token expiring inside the skew window is treated as already expired."""
    oauth = FakeOAuthClient()
    # Default skew is 120s, so 60s of remaining life is not enough.
    service, _ = build_service(config, token=make_token(expires_in=60), oauth=oauth)

    await service.get_access_token("default")
    assert len(oauth.refresh_calls) == 1


async def test_rotation_persists_both_halves_atomically(config):
    """The new access and refresh tokens must land in one write."""
    service, repository = build_service(config, token=make_token(expires_in=-1))

    await service.refresh("default")

    assert len(repository.saves) == 1
    saved = repository.saves[0]
    assert saved.access_token == "access-1"
    assert saved.refresh_token == "refresh-1"


async def test_refresh_takes_the_distributed_lock(config):
    redis = FakeRedis()
    service, _ = build_service(config, token=make_token(expires_in=-1), redis=redis)

    await service.refresh("default")

    assert redis.lock_acquisitions == 1


async def test_concurrent_refreshes_consume_one_refresh_token(config):
    """The critical single-use guarantee.

    Ten callers race on an expired token. Exactly one may reach Oura; the
    others must observe the rotated token after taking the lock.
    """
    oauth = FakeOAuthClient()
    redis = FakeRedis()
    service, repository = build_service(
        config, token=make_token(expires_in=-1), redis=redis, oauth=oauth
    )

    results = await asyncio.gather(*(service.get_access_token("default") for _ in range(10)))

    assert len(oauth.refresh_calls) == 1, "a second replica burned the single-use refresh token"
    assert len(set(results)) == 1, "callers disagreed about the current access token"
    assert results[0] == "access-1"
    assert len(repository.saves) == 1


async def test_refresh_rereads_token_after_acquiring_lock(config):
    """A caller that queued behind another replica must not refresh again."""
    oauth = FakeOAuthClient()
    service, repository = build_service(
        config, token=make_token(expires_in=-1), oauth=oauth
    )
    stale_version = repository.token.version

    await service.refresh("default")
    assert len(oauth.refresh_calls) == 1

    # A second caller arrives holding the version from before the rotation.
    await service.refresh("default", seen_version=stale_version)

    assert len(oauth.refresh_calls) == 1, "the re-read under the lock did not take effect"


async def test_refresh_fails_safely_when_redis_is_unavailable(config):
    """Without the lock we cannot rule out a concurrent refresh, so refuse."""
    oauth = FakeOAuthClient()
    service, _ = build_service(
        config, token=make_token(expires_in=-1), redis=FakeRedis(available=False), oauth=oauth
    )

    with pytest.raises(TokenRefreshUnavailableError):
        await service.refresh("default")

    assert oauth.refresh_calls == [], "refreshed without holding the lock"


async def test_refresh_refuses_when_lock_cannot_be_acquired_in_time(config):
    redis = FakeRedis()

    class Blocked(FakeRedis):
        def lock(self, name, **kwargs):
            class _Lock:
                async def acquire(self) -> bool:
                    return False

                async def release(self) -> None:  # pragma: no cover
                    pass

            return _Lock()

    blocked = Blocked()
    blocked.store = redis.store
    service, _ = build_service(config, token=make_token(expires_in=-1), redis=blocked)

    with pytest.raises(TokenRefreshUnavailableError):
        await service.refresh("default")


async def test_lock_is_released_after_a_failed_refresh(config):
    """A failing Oura call must not leave the lock held for other replicas."""
    oauth = FakeOAuthClient()
    oauth.error = RuntimeError("oura exploded")
    redis = FakeRedis()
    service, _ = build_service(
        config, token=make_token(expires_in=-1), redis=redis, oauth=oauth
    )

    with pytest.raises(RuntimeError):
        await service.refresh("default")

    lock = redis.locks.get("oura:lock:refresh:default")
    assert lock is not None and not lock.locked()


async def test_missing_token_reports_not_connected(config):
    service, _ = build_service(config, token=None)

    with pytest.raises(OuraNotConnectedError) as excinfo:
        await service.get_access_token("default")

    assert "oauth/authorize" in str(excinfo.value)


async def test_redis_error_from_lock_is_not_leaked(config):
    """Callers see a domain error, never a raw RedisError."""
    service, _ = build_service(
        config, token=make_token(expires_in=-1), redis=FakeRedis(available=False)
    )

    with pytest.raises(TokenRefreshUnavailableError):
        await service.refresh("default")

    with pytest.raises(TokenRefreshUnavailableError):
        try:
            await service.refresh("default")
        except RedisError:  # pragma: no cover - would mean a leak
            pytest.fail("RedisError escaped TokenService")
