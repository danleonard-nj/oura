"""Historical snapshots of Oura resources the live API cannot show history for.

`SyncService` polls ring configuration, firmware/battery level and personal
info on a cadence and stores only the snapshots that changed (see
`SnapshotService`). This is the read side `oura_resource_history` calls
through, the same role `OuraService` plays for live Oura data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.models.config import OuraConfig
from app.models.errors import OuraError
from app.models.resources import Resource
from app.repositories.snapshot_repository import SnapshotRepository

#: Resources the sync actually tracks. Anything else is a caller error -- it
#: was never asked to snapshot it.
TRACKED_RESOURCES = (
    Resource.RING_CONFIGURATION,
    Resource.RING_BATTERY_LEVEL,
    Resource.PERSONAL_INFO,
)

DEFAULT_LIMIT = 100


class HistoryService:
    def __init__(self, repository: SnapshotRepository, oura: OuraConfig) -> None:
        self._repository = repository
        self._account_id = oura.account_id

    async def get_snapshots(
        self,
        resource: Resource,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        """Snapshots of `resource` first seen within `since`..`until`, oldest first.

        Both bounds are inclusive. A bare date as `until` covers that whole day.
        """
        if resource not in TRACKED_RESOURCES:
            raise OuraError(
                f"{resource.value} has no tracked history. Only these are tracked: "
                f"{', '.join(r.value for r in TRACKED_RESOURCES)}.",
                status_code=400,
            )

        return await self._repository.recent(
            resource.value,
            account_id=self._account_id,
            since=_parse_bound(since, "since", end_of_day=False),
            until=_parse_bound(until, "until", end_of_day=True),
            limit=limit,
        )


def _parse_bound(value: str | None, name: str, *, end_of_day: bool) -> datetime | None:
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
