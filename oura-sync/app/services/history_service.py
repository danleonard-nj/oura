"""History of Oura resources, read from what the sync stored.

The sync keeps two kinds of history: a change log of resources Oura reports
only the current value of -- ring configuration/firmware and the profile
(`SnapshotService`) -- and an archive of every record of everything else
(`RecordService`, filled backwards by `BackfillService`). This is the read
side `oura_resource_history` and `/api/history/*` call through, the same
role `OuraService` plays for live Oura data.

It answers from Mongo, never from Oura, so it reaches past Oura's own
retention (battery samples above all) and costs no upstream calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.models.config import OuraConfig, SyncConfig
from app.models.errors import OuraError
from app.models.resources import Resource, spec_for
from app.repositories.record_repository import RecordRepository
from app.repositories.snapshot_repository import SnapshotRepository

#: Change-log entries are few; a hundred is years of firmware updates.
DEFAULT_LIMIT = 100
#: Records -- heart rate and battery samples especially -- are many and small.
DEFAULT_RECORD_LIMIT = 1000
MAX_LIMIT = 5000


class HistoryService:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        records: RecordRepository,
        oura: OuraConfig,
        sync: SyncConfig,
    ) -> None:
        self._snapshots = snapshots
        self._records = records
        self._account_id = oura.account_id
        self._synced = set(sync.resources)

    @property
    def tracked_resources(self) -> list[Resource]:
        """The resources this deployment syncs, and so has history for."""
        return [r for r in Resource if r in self._synced]

    async def get_history(
        self,
        resource: Resource,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Stored history of `resource` within `since`..`until`, oldest first.

        Change-log snapshots for current-state resources (ring configuration,
        profile); archived records for everything else -- the most recent
        `limit` of them, if the window holds more. Both bounds are inclusive,
        and a bare date as `until` covers that whole day.
        """
        if resource not in self._synced:
            raise OuraError(
                f"{resource.value} is not synced by this deployment, so there is no stored "
                f"history for it. Synced: {', '.join(r.value for r in self.tracked_resources)}.",
                status_code=400,
            )

        lower = parse_bound(since, "since", end_of_day=False)
        upper = parse_bound(until, "until", end_of_day=True)

        if spec_for(resource).current_state:
            return await self._snapshots.recent(
                resource.value, account_id=self._account_id, since=lower, until=upper,
                limit=_bounded(limit, DEFAULT_LIMIT),
            )
        return await self._records.recent(
            resource.value, account_id=self._account_id, since=lower, until=upper,
            limit=_bounded(limit, DEFAULT_RECORD_LIMIT),
        )


def _bounded(limit: int | None, default: int) -> int:
    if limit is None:
        return default
    return max(1, min(limit, MAX_LIMIT))


def parse_bound(value: str | None, name: str, *, end_of_day: bool) -> datetime | None:
    if not value:
        return None

    try:
        day = date.fromisoformat(value)
    except ValueError:
        day = None

    if day is not None:
        if end_of_day:
            return datetime.combine(day + timedelta(days=1), time.min, UTC) - timedelta(microseconds=1)
        return datetime.combine(day, time.min, UTC)

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OuraError(
            f"{name} must be an ISO-8601 date (YYYY-MM-DD) or datetime, got {value!r}.",
            status_code=400,
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
