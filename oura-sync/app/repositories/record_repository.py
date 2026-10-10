"""Durable copies of Oura records, one document per record.

Oura only keeps some data for a short while -- ring battery samples above
all -- so the sync copies every record it sees here before it disappears.
Records are upserted by a stable key (Oura's `id`, or the timestamp for time
series that have none), so overlapping sync windows never duplicate anything
and a record Oura revises is updated in place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.asynchronous.database import AsyncDatabase

from app.utilities.mongo import OURA_RECORDS


class RecordRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[OURA_RECORDS]

    async def upsert_many(
        self, account_id: str, resource: str, records: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Store `records`, returning (inserted, updated).

        Each entry carries `record_key`, `observed_at`, `content_hash` and
        `payload`. An unchanged record is a no-op write, so `updated` counts
        only records whose content actually changed.
        """
        if not records:
            return 0, 0

        now = datetime.now(UTC)
        operations = [
            UpdateOne(
                {"account_id": account_id, "resource": resource, "record_key": record["record_key"]},
                {
                    "$set": {
                        "payload": record["payload"],
                        "content_hash": record["content_hash"],
                        "observed_at": record["observed_at"],
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
            for record in records
        ]
        result = await self._collection.bulk_write(operations, ordered=False)
        return result.upserted_count, result.modified_count

    async def recent(
        self,
        resource: str,
        *,
        account_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """The latest `limit` records in the window, returned oldest first."""
        query: dict[str, Any] = {"account_id": account_id, "resource": resource}
        span: dict[str, Any] = {}
        if since is not None:
            span["$gte"] = since
        if until is not None:
            span["$lte"] = until
        if span:
            query["observed_at"] = span

        cursor = (
            self._collection.find(query, projection={"_id": False})
            .sort("observed_at", -1)
            .limit(limit)
        )
        documents = [document async for document in cursor]
        documents.reverse()
        return documents
