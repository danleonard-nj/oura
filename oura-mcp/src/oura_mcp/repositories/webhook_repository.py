"""Durable record of received webhook events.

Redis handles fast in-window deduplication; this collection is the durable
history, useful for reconciling a period when the service was down. Oura
webhooks carry no health data, so nothing sensitive is stored here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from oura_mcp.infrastructure.mongo import WEBHOOK_EVENTS
from oura_mcp.models.webhooks import WebhookRecord


class WebhookRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self._collection = database[WEBHOOK_EVENTS]

    async def record(self, event: WebhookRecord) -> bool:
        """Insert an event. Returns False if it was already recorded.

        `dedupe_key` is uniquely indexed, so a redelivery that slipped past the
        Redis window is still caught here.
        """
        try:
            await self._collection.insert_one(event.model_dump(mode="python"))
            return True
        except DuplicateKeyError:
            return False

    async def recent(self, *, limit: int = 50, since_hours: int | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if since_hours is not None:
            query["received_at"] = {"$gte": datetime.now(UTC) - timedelta(hours=since_hours)}

        cursor = (
            self._collection.find(query, projection={"_id": False})
            .sort("received_at", -1)
            .limit(limit)
        )
        return [document async for document in cursor]
