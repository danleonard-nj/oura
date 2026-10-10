"""Short-retention log of every HTTP request that reaches the app."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.utilities.mongo import REQUEST_LOG
from app.models.request_log import RequestLogEntry


class RequestLogRepository:
    """Writes `request_log` and reads recent history back for the admin API.

    Append-only and TTL-pruned (see `ensure_indexes`), unlike `AuditRepository`:
    this is a firehose of every request, most of it opportunistic bot noise,
    kept just long enough to be useful and no longer.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[REQUEST_LOG]

    async def record(self, entry: RequestLogEntry) -> None:
        """Insert one entry. Raises on failure; callers decide how to react."""
        await self._collection.insert_one(entry.model_dump(mode="python", exclude_none=True))

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = (
            self._collection.find({}, projection={"_id": False})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return [_json_safe(document) async for document in cursor]


def _json_safe(document: dict[str, Any]) -> dict[str, Any]:
    """`JSONResponse` can't serialise the naive `datetime` PyMongo hands back."""
    timestamp = document.get("timestamp")
    if isinstance(timestamp, datetime):
        return {**document, "timestamp": timestamp.isoformat()}
    return document
