"""Keep a change log of resources Oura reports only the current value of.

Ring configuration (model, firmware) and the profile have no history behind
them in Oura's API: a firmware update or a new weight simply replaces what was
there. Each sync fetches the current value and stores it only if it differs
from the last snapshot, so `resource_snapshots` answers "when did this change".

Reuses `OuraService` for the fetch, so token refresh, the 401 retry-once path
and error translation are the same code the MCP tools run on.
"""

from __future__ import annotations

from app.models.dates import DateRange
from app.models.resources import Resource
from app.repositories.snapshot_repository import SnapshotRepository
from app.services.oura_service import OuraService
from app.services.record_service import content_hash


class SnapshotService:
    def __init__(self, oura: OuraService, repository: SnapshotRepository) -> None:
        self._oura = oura
        self._repository = repository

    async def sync_one(self, resource: Resource, date_range: DateRange) -> dict:
        """Fetch `resource` live and store it if it differs from the last snapshot.

        Always bypasses the response cache -- the whole point is a live read
        that can actually detect a change, not a cached one from the last poll.
        """
        if resource is Resource.PERSONAL_INFO:
            fetch = await self._oura.get_personal_info(use_cache=False)
            payload = fetch.records
        else:
            fetch = await self._oura.get_records(resource, date_range, use_cache=False)
            payload = fetch.records if isinstance(fetch.records, list) else [fetch.records]
            if not payload:
                return {"resource": resource.value, "changed": False, "reason": "no records in window"}

        _document, changed = await self._repository.upsert_if_changed(
            self._oura.account_id, resource.value, payload, content_hash(payload)
        )
        return {"resource": resource.value, "changed": changed}
