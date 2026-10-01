"""Durable storage for `ResourceSnapshot` documents.

Same Mongo database `oauth_tokens`/`audit_events` already live in -- no new
datastore, just a new collection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

RESOURCE_SNAPSHOTS = "resource_snapshots"


async def ensure_indexes(database: AsyncDatabase) -> None:
    await database[RESOURCE_SNAPSHOTS].create_index(
        [("account_id", 1), ("resource", 1), ("first_seen_at", -1)]
    )


class SnapshotRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[RESOURCE_SNAPSHOTS]

    async def latest(self, account_id: str, resource: str) -> dict[str, Any] | None:
        cursor = (
            self._collection.find({"account_id": account_id, "resource": resource})
            .sort("first_seen_at", -1)
            .limit(1)
        )
        async for document in cursor:
            return document
        return None

    async def upsert_if_changed(
        self, account_id: str, resource: str, payload: Any, content_hash: str
    ) -> tuple[dict[str, Any], bool]:
        """Insert a new snapshot if the payload changed, else touch `last_seen_at`.

        Returns the current document (new or existing) and whether it changed.
        """
        now = datetime.now(UTC)
        current = await self.latest(account_id, resource)

        if current is not None and current["content_hash"] == content_hash:
            await self._collection.update_one(
                {"_id": current["_id"]}, {"$set": {"last_seen_at": now}}
            )
            current["last_seen_at"] = now
            return current, False

        document = {
            "account_id": account_id,
            "resource": resource,
            "content_hash": content_hash,
            "payload": payload,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        result = await self._collection.insert_one(document)
        document["_id"] = result.inserted_id
        return document, True

    async def recent(
        self,
        resource: str,
        *,
        account_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"account_id": account_id, "resource": resource}
        span: dict[str, Any] = {}
        if since is not None:
            span["$gte"] = since
        if until is not None:
            span["$lte"] = until
        if span:
            query["first_seen_at"] = span

        cursor = (
            self._collection.find(query, projection={"_id": False})
            .sort("first_seen_at", 1)
            .limit(limit)
        )
        return [document async for document in cursor]
