"""Historical snapshots of Oura resources the live API cannot show history for.

Backed by oura-sync, which polls ring configuration, firmware/battery level
and personal info on a cadence and stores only the snapshots that changed
(see oura-sync's SnapshotService). This is the seam `oura_resource_history`
calls through, the same role `OuraService` plays for live Oura data.
"""

from __future__ import annotations

import httpx2

from oura_common.models.errors import OuraError, OuraUpstreamError
from oura_common.models.resources import Resource

from oura_mcp.clients.sync_client import SyncServiceClient, SyncServiceNotConfiguredError
from oura_mcp.config import Config

#: Resources oura-sync actually tracks. Anything else is a caller error, not
#: an upstream one -- oura-sync was never asked to snapshot it.
TRACKED_RESOURCES = (
    Resource.RING_CONFIGURATION,
    Resource.RING_BATTERY_LEVEL,
    Resource.PERSONAL_INFO,
)


class HistoryService:
    def __init__(self, client: SyncServiceClient, config: Config) -> None:
        self._client = client
        self._account_id = config.oura_account_id

    async def get_snapshots(
        self, resource: Resource, *, since: str | None = None, until: str | None = None
    ) -> list[dict]:
        if resource not in TRACKED_RESOURCES:
            raise OuraError(
                f"{resource.value} has no tracked history. oura-sync only tracks: "
                f"{', '.join(r.value for r in TRACKED_RESOURCES)}.",
                status_code=400,
            )

        try:
            return await self._client.get_snapshots(
                resource, self._account_id, since=since, until=until
            )
        except SyncServiceNotConfiguredError as exc:
            raise OuraError(str(exc), status_code=503) from exc
        except httpx2.HTTPStatusError as exc:
            raise OuraUpstreamError(
                f"oura-sync returned HTTP {exc.response.status_code} fetching "
                f"{resource.value} history.",
                status_code=502,
            ) from exc
        except httpx2.HTTPError as exc:
            raise OuraUpstreamError(
                f"Could not reach oura-sync to fetch {resource.value} history."
            ) from exc
