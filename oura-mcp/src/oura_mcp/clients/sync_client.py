"""HTTP client for oura-sync's snapshot-query API.

oura-sync tracks resources Oura's live API cannot show history for (ring
configuration, firmware/battery level, personal info) by polling on a cadence
and storing only the snapshots that changed. This is how oura-mcp asks for
that history: an Azure AD client-credentials token scoped to oura-sync's app
registration, the same way the other internal services authenticate to each
other through the API gateway.
"""

from __future__ import annotations

from typing import Any

from httpx2 import AsyncClient

from oura_common.azure_ad import AzureAdError, AzureAdTokenClient
from oura_common.models.resources import Resource

from oura_mcp.config import Config


class SyncServiceNotConfiguredError(Exception):
    """SYNC_SERVICE_BASE_URL / AAD_SYNC_SCOPE are not set on this deployment."""


class SyncServiceClient:
    def __init__(self, http: AsyncClient, azure_ad: AzureAdTokenClient, config: Config) -> None:
        self._http = http
        self._azure_ad = azure_ad
        self._base_url = config.sync_service_base_url
        self._scope = config.aad_sync_scope
        self._timeout = config.oura_request_timeout_seconds

    async def get_snapshots(
        self,
        resource: Resource,
        account_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Historical snapshots of one resource, most recent last."""
        if not self._base_url or not self._scope:
            raise SyncServiceNotConfiguredError(
                "oura-sync is not configured on this deployment: set "
                "SYNC_SERVICE_BASE_URL and AAD_SYNC_SCOPE to enable it."
            )

        try:
            token = await self._azure_ad.get_token(self._scope)
        except AzureAdError as exc:
            raise SyncServiceNotConfiguredError(str(exc)) from exc

        params: dict[str, Any] = {"account_id": account_id, "limit": limit}
        if since:
            params["since"] = since
        if until:
            params["until"] = until

        response = await self._http.get(
            f"{self._base_url}/api/snapshots/{resource.value}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json().get("snapshots", [])
