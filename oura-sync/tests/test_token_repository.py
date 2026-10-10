"""Token persistence: encryption at rest and single-write rotation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.utilities.crypto import TokenCipher
from app.repositories.token_repository import TokenRepository
from tests.conftest import make_token


class FakeCollection:
    """Records the update operations issued against it."""

    def __init__(self) -> None:
        self.document: dict | None = None
        self.updates: list[dict] = []

    async def find_one(self, query, projection=None):
        if self.document is None:
            return None
        if projection:
            return {k: v for k, v in self.document.items() if projection.get(k)}
        return dict(self.document)

    async def find_one_and_update(self, query, update, *, upsert=False, return_document=True):
        self.updates.append(update)
        document = dict(self.document or {})
        document.update(update.get("$set", {}))
        if self.document is None:
            document.update(update.get("$setOnInsert", {}))
        for field, amount in update.get("$inc", {}).items():
            document[field] = document.get(field, 0) + amount
        self.document = document
        return dict(document)

    async def delete_one(self, query):
        existed = self.document is not None
        self.document = None
        return type("Result", (), {"deleted_count": 1 if existed else 0})()


class FakeDatabase(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeCollection())


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def repository(database, config) -> TokenRepository:
    return TokenRepository(database, TokenCipher(config.tokens))  # type: ignore[arg-type]


async def test_tokens_are_encrypted_before_they_reach_mongo(repository, database):
    await repository.save(make_token(access="plaintext-access", refresh="plaintext-refresh"))

    stored = database["oauth_tokens"].document
    assert stored["access_token"] != "plaintext-access"
    assert stored["refresh_token"] != "plaintext-refresh"
    assert "plaintext-access" not in str(stored)
    assert "plaintext-refresh" not in str(stored)


async def test_a_saved_token_reads_back_as_plaintext(repository):
    await repository.save(make_token(access="a1", refresh="r1"))

    loaded = await repository.get("default")

    assert loaded is not None
    assert loaded.access_token == "a1"
    assert loaded.refresh_token == "r1"


async def test_rotation_is_a_single_atomic_update(repository, database):
    """Both halves must move together; no window with a mismatched pair."""
    await repository.save(make_token(access="a1", refresh="r1"))
    await repository.save(make_token(access="a2", refresh="r2"))

    assert len(database["oauth_tokens"].updates) == 2
    rotation = database["oauth_tokens"].updates[-1]
    assert "access_token" in rotation["$set"]
    assert "refresh_token" in rotation["$set"]
    assert rotation["$inc"] == {"version": 1}

    loaded = await repository.get("default")
    assert (loaded.access_token, loaded.refresh_token) == ("a2", "r2")


async def test_version_increments_on_every_save(repository):
    first = await repository.save(make_token())
    second = await repository.save(make_token())

    assert first.version == 1
    assert second.version == 2


async def test_status_never_projects_token_material(repository, database):
    await repository.save(make_token(access="secret-access", refresh="secret-refresh"))

    status = await repository.status("default")

    dumped = status.model_dump_json()
    assert "secret-access" not in dumped
    assert "secret-refresh" not in dumped
    assert status.connected is True
    assert "daily" in status.scopes


async def test_status_for_an_unknown_account(repository):
    status = await repository.status("nobody")

    assert status.connected is False
    assert status.expires_at is None


async def test_naive_timestamps_from_mongo_are_treated_as_utc(repository, database):
    await repository.save(make_token())
    database["oauth_tokens"].document["expires_at"] = datetime(2026, 9, 1, 12, 0, 0)

    loaded = await repository.get("default")

    assert loaded.expires_at.tzinfo is UTC


async def test_delete_removes_the_connection(repository):
    await repository.save(make_token())

    assert await repository.delete("default") is True
    assert await repository.get("default") is None
    assert await repository.delete("default") is False
