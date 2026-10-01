"""Fetch one resource live from Oura and store it only if it changed.

Reuses `OuraService` (oura-common) for the actual fetch, so token refresh,
the 401 retry-once path and error translation are the same code oura-mcp
runs on -- this only adds the hash-diff-and-store step on top.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from oura_common.models.dates import DateRange, today_utc
from oura_common.models.resources import Resource
from oura_common.services.oura_service import OuraService

from oura_sync.repositories.snapshot_repository import SnapshotRepository


class SnapshotService:
    def __init__(self, oura: OuraService, repository: SnapshotRepository) -> None:
        self._oura = oura
        self._repository = repository

    async def sync_one(self, resource: Resource) -> dict:
        """Fetch `resource` live and store it if it differs from the last snapshot.

        Always bypasses the response cache -- the whole point is a live read
        that can actually detect a change, not a cached one from the last poll.
        """
        account_id = self._oura.account_id

        if resource is Resource.PERSONAL_INFO:
            fetch = await self._oura.get_personal_info(use_cache=False)
            payload = fetch.records
        else:
            today: date = today_utc()
            fetch = await self._oura.get_records(
                resource, DateRange(start=today, end=today), use_cache=False
            )
            records = fetch.records if isinstance(fetch.records, list) else []
            if not records:
                return {"resource": resource.value, "changed": False, "reason": "no data yet today"}
            # The most recent record Oura has for today; a day can have more
            # than one entry (e.g. battery level sampled repeatedly).
            payload = records[-1]

        content_hash = _hash(payload)
        _document, changed = await self._repository.upsert_if_changed(
            account_id, resource.value, payload, content_hash
        )
        return {"resource": resource.value, "changed": changed}


def _hash(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
