"""Shared fixtures.

Nothing here touches a real Oura account, Mongo instance or Redis server. The
fakes below implement only the surface the code under test actually uses, so a
change in what the code needs shows up as a test failure rather than passing
silently against an over-permissive mock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import httpx2
from cryptography.fernet import Fernet

from oura_mcp.config import Config
from oura_mcp.models.tokens import OuraToken, TokenGrant

@pytest.fixture
def config() -> Config:
    return Config(
        oura_client_id="test-client-id",
        oura_client_secret="test-client-secret",  # noqa: S106 - fixture value
        oura_redirect_uri="https://oura.test/oauth/callback",
        mcp_api_key="test-mcp-api-key",  # noqa: S106 - fixture value
        token_encryption_key=Fernet.generate_key().decode(),
        mongodb_uri="mongodb://mongo.invalid:27017",
        redis_url="redis://redis.invalid:6379/0",
        public_host="oura.test",
        oura_webhook_verification_token="test-verification-token",  # noqa: S106
        log_level="WARNING",
    )


# ── Fakes ─────────────────────────────────────────────────────────────────



class FakeRedis:
    """In-memory stand-in covering the commands the services use."""

    def __init__(self, *, available: bool = True) -> None:
        self.store: dict[str, str] = {}
        self.available = available
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_acquisitions = 0

    def _guard(self) -> None:
        if not self.available:
            from redis.exceptions import RedisError

            raise RedisError("redis is down")

    async def get(self, key: str) -> str | None:
        self._guard()
        return self.store.get(key)

    async def set(self, key, value, *, ex=None, nx=False) -> bool | None:
        self._guard()
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    async def getdel(self, key: str) -> str | None:
        self._guard()
        return self.store.pop(key, None)

    async def incr(self, key: str) -> int:
        self._guard()
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def ping(self) -> bool:
        self._guard()
        return True

    def lock(self, name, *, timeout=None, blocking=True, blocking_timeout=None, **_):
        self._guard()
        redis = self

        class _Lock:
            async def acquire(self) -> bool:
                redis._guard()
                lock = redis.locks.setdefault(name, asyncio.Lock())
                if lock.locked() and not blocking:
                    return False
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=blocking_timeout)
                except TimeoutError:
                    return False
                redis.lock_acquisitions += 1
                return True

            async def release(self) -> None:
                lock = redis.locks.get(name)
                if lock is not None and lock.locked():
                    lock.release()

        return _Lock()


class FakeTokenRepository:
    """Token store that keeps plaintext in memory and counts writes."""

    def __init__(self, token: OuraToken | None = None) -> None:
        self.token = token
        self.saves: list[OuraToken] = []

    async def get(self, account_id: str) -> OuraToken | None:
        return self.token

    async def save(self, token: OuraToken) -> OuraToken:
        version = (self.token.version + 1) if self.token else 1
        stored = token.model_copy(update={"version": version, "updated_at": datetime.now(UTC)})
        self.token = stored
        self.saves.append(stored)
        return stored

    async def status(self, account_id: str):
        from oura_mcp.models.tokens import ConnectionStatus

        if self.token is None:
            return ConnectionStatus(account_id=account_id, connected=False)
        return ConnectionStatus(
            account_id=account_id,
            connected=True,
            expires_at=self.token.expires_at,
            scopes=self.token.scopes,
            version=self.token.version,
        )

    async def delete(self, account_id: str) -> bool:
        existed = self.token is not None
        self.token = None
        return existed


class FakeAuditRepository:
    def __init__(self, *, failing: bool = False) -> None:
        self.events: list[Any] = []
        self.failing = failing

    async def record(self, event) -> None:
        if self.failing:
            raise RuntimeError("mongo is down")
        self.events.append(event)

    async def recent(self, account_id, *, limit=50, since_hours=None):
        return [e.model_dump(mode="python") for e in self.events][:limit]


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def record(self, event) -> bool:
        if any(r.dedupe_key == event.dedupe_key for r in self.records):
            return False
        self.records.append(event)
        return True

    async def recent(self, *, limit=50, since_hours=None):
        return [r.model_dump(mode="python") for r in self.records][:limit]


class FakeOAuthClient:
    """Issues predictable, rotating grants and counts refreshes."""

    def __init__(self) -> None:
        self.refresh_calls: list[str] = []
        self.exchange_calls: list[str] = []
        self.counter = 0
        self.error: Exception | None = None

    def _next_grant(self) -> TokenGrant:
        self.counter += 1
        return TokenGrant(
            access_token=f"access-{self.counter}",
            refresh_token=f"refresh-{self.counter}",
            expires_in=3600,
            scope="daily personal",
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        if self.error:
            raise self.error
        self.refresh_calls.append(refresh_token)
        return self._next_grant()

    async def exchange_code(self, code: str) -> TokenGrant:
        if self.error:
            raise self.error
        self.exchange_calls.append(code)
        return self._next_grant()

    def authorization_url(self, state: str) -> str:
        return f"https://cloud.ouraring.com/oauth/authorize?state={state}"


# ── Helpers ───────────────────────────────────────────────────────────────


def make_token(
    *,
    account_id: str = "default",
    expires_in: int = 3600,
    version: int = 1,
    access: str = "access-0",
    refresh: str = "refresh-0",
) -> OuraToken:
    return OuraToken(
        account_id=account_id,
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=["daily", "personal"],
        version=version,
    )


def http_error(
    status: int,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> httpx2.HTTPStatusError:
    """Build the exception the client raises via `raise_for_status()`."""
    response = httpx2.Response(
        status,
        headers=headers or {},
        json=json_body if json_body is not None else {},
        request=httpx2.Request("GET", "https://api.ouraring.com/v2/usercollection/x"),
    )
    return httpx2.HTTPStatusError(
        f"{status} error", request=response.request, response=response
    )


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def audit_repository() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest.fixture
def audit_service(audit_repository: FakeAuditRepository):
    from oura_mcp.services.audit_service import AuditService

    return AuditService(audit_repository)  # type: ignore[arg-type]
