"""Shared fixtures.

Nothing here touches a real Oura account, Mongo instance or Redis server. The
fakes below implement only the surface the code under test actually uses, so a
change in what the code needs shows up as a test failure rather than passing
silently against an over-permissive mock.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import httpx2
from cryptography.fernet import Fernet

from app.models.tokens import OuraToken, TokenGrant

from app.models.config import SECTIONS, bind_sections


def base_config_values() -> dict:
    """A config.json for tests: required settings plus the ones tests rely on."""
    return {
        "service": {"public_host": "oura.test", "log_level": "WARNING"},
        "oura": {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",  # noqa: S106 - fixture value
            "redirect_uri": "https://oura.test/oauth/callback",
        },
        "tokens": {"encryption_key": Fernet.generate_key().decode()},
        "mongo": {"connection_string": "mongodb://mongo.invalid:27017"},
        "redis": {"connection_string": "redis://redis.invalid:6379/0"},
        "webhooks": {"verification_token": "test-verification-token"},  # noqa: S106
        "mcp": {"api_key": "test-mcp-api-key"},  # noqa: S106 - fixture value
        # No background sync, backfill or wear poll in tests -- they trigger them explicitly.
        "sync": {"interval_hours": 0, "backfill_enabled": False},
        "wear": {"poll_interval_minutes": 0},
    }


class Sections:
    """The bound config sections, by config.json key: `config.oura`, `config.mcp`, ...

    `values` is the raw config.json dict they were validated from, which is
    what a framework `Configuration` would carry.
    """

    def __init__(self, values: dict) -> None:
        self.values = values
        for section_type, section in bind_sections(values).items():
            setattr(self, SECTIONS[section_type], section)

    def replace(self, key: str, **changes) -> "Sections":
        """A copy with one section changed -- re-validated, like a real file."""
        values = copy.deepcopy(self.values)
        values.setdefault(key, {}).update(changes)
        return Sections(values)


@pytest.fixture
def config() -> Sections:
    return Sections(base_config_values())


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

    async def delete(self, *keys: str) -> int:
        self._guard()
        return sum(self.store.pop(key, None) is not None for key in keys)

    async def expire(self, key: str, _seconds: int) -> bool:
        self._guard()
        return key in self.store

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
        from app.models.tokens import ConnectionStatus

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


class FakeRequestLogRepository:
    def __init__(self, *, failing: bool = False) -> None:
        self.entries: list[Any] = []
        self.failing = failing

    async def record(self, entry) -> None:
        if self.failing:
            raise RuntimeError("mongo is down")
        self.entries.append(entry)

    async def recent(self, *, limit: int = 50):
        return [e.model_dump(mode="json") for e in self.entries][:limit]


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


class FakeCollection:
    """In-memory stand-in covering the operations the repositories issue."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self._next_id = 1

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        matches = [d for d in self.documents if _matches(d, query)]
        return _Cursor(matches, projection)

    async def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        async for document in self.find(query, projection):
            return document
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> None:
        for document in self.documents:
            if _matches(document, query):
                document.update(update.get("$set", {}))
                return
        if upsert:
            await self.insert_one({**query, **update.get("$set", {})})

    async def bulk_write(self, operations, ordered: bool = True):
        """UpdateOne operations only -- all RecordRepository issues."""
        upserted = modified = 0
        for operation in operations:
            query, update = operation._filter, operation._doc
            match = next((d for d in self.documents if _matches(d, query)), None)
            if match is None:
                if operation._upsert:
                    await self.insert_one(
                        {**query, **update.get("$setOnInsert", {}), **update.get("$set", {})}
                    )
                    upserted += 1
                continue
            changes = update.get("$set", {})
            if any(match.get(k) != v for k, v in changes.items()):
                match.update(changes)
                modified += 1
        return type("Result", (), {"upserted_count": upserted, "modified_count": modified})()

    async def delete_one(self, query: dict[str, Any]):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                del self.documents[index]
                return type("Result", (), {"deleted_count": 1})()
        return type("Result", (), {"deleted_count": 0})()

    async def insert_one(self, document: dict[str, Any]):
        document = dict(document)
        document["_id"] = self._next_id
        self._next_id += 1
        self.documents.append(document)
        return type("Result", (), {"inserted_id": document["_id"]})()

    async def create_index(self, *_args, **_kwargs) -> None:
        return None


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]], projection: dict[str, Any] | None) -> None:
        self._documents = documents
        self._projection = projection or {}
        self._sort: tuple[str, int] | None = None
        self._limit: int | None = None

    def sort(self, field: str, direction: int) -> "_Cursor":
        self._sort = (field, direction)
        return self

    def limit(self, count: int) -> "_Cursor":
        self._limit = count
        return self

    def _ordered(self) -> list[dict[str, Any]]:
        documents = list(self._documents)
        if self._sort:
            field, direction = self._sort
            documents.sort(key=lambda d: d[field], reverse=direction < 0)
        if self._limit is not None:
            documents = documents[: self._limit]
        return documents

    def __aiter__(self):
        self._iter = iter(self._ordered())
        return self

    async def __anext__(self):
        try:
            document = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration
        if self._projection.get("_id") is False:
            document = {k: v for k, v in document.items() if k != "_id"}
        return document


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        value = document.get(key)
        if isinstance(expected, dict):
            if "$gte" in expected and value < expected["$gte"]:
                return False
            if "$lte" in expected and value > expected["$lte"]:
                return False
        elif value != expected:
            return False
    return True


class FakeDatabase(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeCollection())


# ── Helpers ───────────────────────────────────────────────────────────────


def make_token(
    *,
    account_id: str = "default",
    expires_in: int = 3600,
    version: int = 1,
    access: str = "access-0",
    refresh: str = "refresh-0",
    scopes: list[str] | None = None,
) -> OuraToken:
    """A stored token.

    Scopes default to everything the service asks for, because `OuraService`
    now refuses locally when a resource's scope is absent. Pass a narrower list
    to exercise that path.
    """
    from app.models.config import DEFAULT_SCOPES

    return OuraToken(
        account_id=account_id,
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=scopes if scopes is not None else DEFAULT_SCOPES.split(),
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
def request_log_repository() -> FakeRequestLogRepository:
    return FakeRequestLogRepository()


@pytest.fixture
def audit_service(audit_repository: FakeAuditRepository):
    from app.services.audit_service import AuditService

    return AuditService(audit_repository)  # type: ignore[arg-type]


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


def utc_now() -> datetime:
    return datetime.now(UTC)
