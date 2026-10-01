"""Shared fixtures. Nothing here touches a real Oura account, Mongo or Redis."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet

from oura_sync.config import Config


@pytest.fixture
def config() -> Config:
    return Config(
        oura_client_id="test-client-id",
        oura_client_secret="test-client-secret",  # noqa: S106 - fixture value
        oura_redirect_uri="https://oura.test/oauth/callback",
        token_encryption_key=Fernet.generate_key().decode(),
        mongodb_uri="mongodb://mongo.invalid:27017",
        redis_url="redis://redis.invalid:6379/0",
        log_level="WARNING",
    )


class FakeCollection:
    """In-memory stand-in covering the operations SnapshotRepository issues."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self._next_id = 1

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        matches = [d for d in self.documents if _matches(d, query)]
        return _Cursor(matches, projection)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        for document in self.documents:
            if _matches(document, query):
                document.update(update.get("$set", {}))
                return

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


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


def utc_now() -> datetime:
    return datetime.now(UTC)
