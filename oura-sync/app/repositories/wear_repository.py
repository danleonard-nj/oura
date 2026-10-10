"""Ring wear persistence: the evidence, the current judgement, and its changes.

* `wear_days` -- one document per Oura activity day: its 04:00-local start
  and the `class_5_min` string, which is all the wear history there is. A
  day is only replaced by one covering at least as many slots, so a short or
  partial response never erases evidence already stored.
* `wear_status` -- one document per account: the latest judgement and the
  evidence timestamps behind it.
* `wear_transitions` -- each change between WORN and NOT_WORN, keyed by the
  evidence time it happened at, so re-detecting the same change is a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.utilities.mongo import WEAR_DAYS, WEAR_STATUS, WEAR_TRANSITIONS


class WearRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self._days = database[WEAR_DAYS]
        self._status = database[WEAR_STATUS]
        self._transitions = database[WEAR_TRANSITIONS]

    # ── Activity days ─────────────────────────────────────────────────────

    async def save_day(
        self, account_id: str, *, day: str, start: datetime, class_5_min: str, non_wear_seconds: int | None
    ) -> bool:
        """Store a day's classification unless what is stored already covers more.

        Returns whether anything changed. Equal coverage with different
        content is a revision from Oura and replaces what was there.
        """
        query = {"account_id": account_id, "day": day}
        existing = await self._days.find_one(query, projection={"_id": False})
        if existing is not None:
            if len(existing.get("class_5_min") or "") > len(class_5_min):
                return False
            if existing.get("class_5_min") == class_5_min and existing.get("start") == start:
                return False

        await self._days.update_one(
            query,
            {
                "$set": {
                    "start": start,
                    "class_5_min": class_5_min,
                    "slots": len(class_5_min),
                    "non_wear_seconds": non_wear_seconds,
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        return True

    async def days(self, account_id: str, *, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Days starting within `since`..`until`, oldest first."""
        cursor = self._days.find(
            {"account_id": account_id, "start": {"$gte": since, "$lte": until}},
            projection={"_id": False},
        ).sort("start", 1)
        return [document async for document in cursor]

    # ── Current status ────────────────────────────────────────────────────

    async def get_status(self, account_id: str) -> dict[str, Any] | None:
        return await self._status.find_one({"account_id": account_id}, projection={"_id": False})

    async def set_status(self, account_id: str, **fields: Any) -> None:
        await self._status.update_one({"account_id": account_id}, {"$set": fields}, upsert=True)

    # ── Transitions ───────────────────────────────────────────────────────

    async def record_transition(self, account_id: str, *, status: str, at: datetime, **fields: Any) -> bool:
        """Store a transition once per (status, at). Returns whether it was new."""
        query = {"account_id": account_id, "status": status, "at": at}
        if await self._transitions.find_one(query) is not None:
            return False
        await self._transitions.update_one(query, {"$set": fields}, upsert=True)
        return True

    async def transitions(
        self, account_id: str, *, since: datetime, until: datetime, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Transitions within `since`..`until`, oldest first."""
        cursor = (
            self._transitions.find(
                {"account_id": account_id, "at": {"$gte": since, "$lte": until}},
                projection={"_id": False},
            )
            .sort("at", 1)
            .limit(limit)
        )
        return [document async for document in cursor]
