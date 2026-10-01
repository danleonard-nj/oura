"""Orchestrates one sync run: every configured resource, for the configured account.

Single account per deployment (`oura.account_id`) -- see the README for why
multi-account is a routing change, not something this service does today.
"""

from __future__ import annotations

from app.utilities.logging import get_logger
from app.models.errors import OuraError
from app.models.resources import Resource

from app.models.config import SyncConfig
from app.services.snapshot_service import SnapshotService

logger = get_logger(__name__)


class SyncService:
    def __init__(self, snapshots: SnapshotService, config: SyncConfig) -> None:
        self._snapshots = snapshots
        self._default_resources = config.resources

    async def sync_all(self, resources: list[Resource] | None = None) -> list[dict]:
        """Sync every requested resource, tolerating a per-resource failure.

        One resource being ungranted or unreachable should not stop the
        others from being polled -- the same reasoning `SummaryService`
        applies to its fan-out.
        """
        results = []
        for resource in resources or self._default_resources:
            try:
                results.append(await self._snapshots.sync_one(resource))
            except OuraError as exc:
                logger.warning(
                    "sync failed for resource",
                    extra={"resource": resource.value, "error": exc.message},
                )
                results.append(
                    {"resource": resource.value, "changed": False, "error": exc.message}
                )
        return results
