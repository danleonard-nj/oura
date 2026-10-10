"""Per-resource sync progress, one document per (account, resource).

Two independent cursors live here:

* `synced_through` -- the last day a regular sync archived successfully, so
  the next run can close any gap an outage left rather than only fetching its
  fixed lookback.
* `backfill` -- how far back the back-sync has walked, where it stops, and
  whether it is done. A restart resumes from here instead of starting over.

Dates are stored as ISO strings: they are calendar days in Oura's sense, not
instants, and a BSON datetime would invite a timezone to creep in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.utilities.mongo import SYNC_STATE


class SyncStateRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[SYNC_STATE]

    async def get(self, account_id: str, resource: str) -> dict[str, Any]:
        """The resource's state, or an empty dict if it has never synced."""
        document = await self._collection.find_one(
            {"account_id": account_id, "resource": resource}, projection={"_id": False}
        )
        return document or {}

    async def all(self, account_id: str) -> list[dict[str, Any]]:
        cursor = self._collection.find({"account_id": account_id}, projection={"_id": False})
        return [document async for document in cursor]

    async def set(self, account_id: str, resource: str, **fields: Any) -> None:
        """Overwrite the named top-level fields, creating the document if needed."""
        await self._collection.update_one(
            {"account_id": account_id, "resource": resource},
            {"$set": {**fields, "updated_at": datetime.now(UTC)}},
            upsert=True,
        )
