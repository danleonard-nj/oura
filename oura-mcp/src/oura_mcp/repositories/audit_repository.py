"""Append-only operational audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from oura_mcp.infrastructure.mongo import AUDIT_EVENTS
from oura_mcp.models.audit import AuditEvent


class AuditRepository:
    """Writes `audit_events` and reads recent history back for the admin API."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[AUDIT_EVENTS]

    async def record(self, event: AuditEvent) -> None:
        """Insert one event. Raises on failure; callers decide how to react."""
        await self._collection.insert_one(event.model_dump(mode="python", exclude_none=True))

    async def recent(
        self, account_id: str, *, limit: int = 50, since_hours: int | None = None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"account_id": account_id}
        if since_hours is not None:
            query["timestamp"] = {"$gte": datetime.now(UTC) - timedelta(hours=since_hours)}

        cursor = (
            self._collection.find(query, projection={"_id": False})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return [document async for document in cursor]
