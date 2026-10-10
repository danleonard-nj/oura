"""Orchestrates one sync run: every configured resource, for the configured account.

Current-state resources (ring configuration, profile) go to the snapshot
change log; everything else is archived record by record. See
`SnapshotService` and `RecordService`. History older than the first sync is
the back-sync's job -- see `BackfillService`.

Single account per deployment (`oura.account_id`) -- see the README for why
multi-account is a routing change, not something this service does today.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.models.config import OuraConfig, SyncConfig
from app.models.dates import DateRange, today_utc
from app.models.errors import OuraError
from app.models.resources import Resource, spec_for
from app.repositories.sync_state_repository import SyncStateRepository
from app.services.record_service import RecordService
from app.services.snapshot_service import SnapshotService
from app.utilities.logging import get_logger

logger = get_logger(__name__)


class SyncService:
    def __init__(
        self,
        snapshots: SnapshotService,
        records: RecordService,
        state: SyncStateRepository,
        config: SyncConfig,
        oura: OuraConfig,
    ) -> None:
        self._snapshots = snapshots
        self._records = records
        self._state = state
        self._default_resources = config.resources
        self._lookback_days = config.lookback_days
        self._account_id = oura.account_id

    async def sync_all(self, resources: list[Resource] | None = None) -> list[dict]:
        """Sync every requested resource, tolerating a per-resource failure.

        One resource being ungranted or unreachable should not stop the
        others from being polled -- the same reasoning `SummaryService`
        applies to its fan-out.
        """
        today = today_utc()
        recent = DateRange(start=today - timedelta(days=self._lookback_days - 1), end=today)

        results = []
        for resource in resources or self._default_resources:
            try:
                if spec_for(resource).current_state:
                    results.append(await self._snapshots.sync_one(resource, recent))
                else:
                    results.append(await self._archive(resource, recent))
            except OuraError as exc:
                logger.warning(
                    "sync failed for resource",
                    extra={"resource": resource.value, "error": exc.message},
                )
                results.append({"resource": resource.value, "error": exc.message})

        logger.info(
            "sync finished",
            extra={
                "resources": len(results),
                "failed": sum(1 for r in results if "error" in r),
                "inserted": sum(r.get("inserted", 0) for r in results),
            },
        )
        return results

    async def _archive(self, resource: Resource, recent: DateRange) -> dict:
        """Archive from the last day this resource synced, or the lookback if later.

        The lookback alone would leave a hole after any outage longer than
        it. Starting from `synced_through` closes that hole; the lookback
        still applies on top, so records Oura revises keep being re-read.
        """
        window = recent
        state = await self._state.get(self._account_id, resource.value)
        if synced_through := state.get("synced_through"):
            window = DateRange(start=min(recent.start, date.fromisoformat(synced_through)), end=recent.end)

        result = await self._records.archive(resource, window)
        await self._state.set(self._account_id, resource.value, synced_through=window.end.isoformat())
        return result
