"""Async Oura API v2 client.

Every collection is the same shape -- `GET /v2/usercollection/{segment}` with
`start_date`/`end_date` and `next_token` pagination -- so the whole surface is
one map plus one pagination loop. That is why `oura-ring` was dropped: it added
a synchronous `requests` dependency, a thread pool to keep it off the event
loop, and coverage gaps for four collections, in exchange for code we can spell
out in eighty lines.

Nothing here blocks. The shared `httpx2.AsyncClient` is a singleton, so
connection pooling and keep-alive work across the whole application rather than
per worker thread.
"""

from __future__ import annotations

from typing import Any

from httpx2 import AsyncClient

from oura_mcp.config import Config
from oura_mcp.models.resources import Resource

API_URL = "https://api.ouraring.com"

#: Resource -> path segment under /v2/usercollection/.
#: Mostly identical to the enum value; VO2 max is the odd one out because Oura
#: spells the path `vO2_max` while its webhook data_type is `vo2_max`.
RESOURCE_PATHS: dict[Resource, str] = {
    Resource.DAILY_SLEEP: "daily_sleep",
    Resource.DAILY_READINESS: "daily_readiness",
    Resource.DAILY_ACTIVITY: "daily_activity",
    Resource.DAILY_STRESS: "daily_stress",
    Resource.DAILY_SPO2: "daily_spo2",
    Resource.DAILY_RESILIENCE: "daily_resilience",
    Resource.DAILY_CARDIOVASCULAR_AGE: "daily_cardiovascular_age",
    Resource.VO2_MAX: "vO2_max",
    Resource.SLEEP: "sleep",
    Resource.SLEEP_TIME: "sleep_time",
    Resource.HEARTRATE: "heartrate",
    Resource.WORKOUT: "workout",
    Resource.SESSION: "session",
    Resource.TAG: "tag",
    Resource.ENHANCED_TAG: "enhanced_tag",
    Resource.REST_MODE_PERIOD: "rest_mode_period",
    Resource.RING_CONFIGURATION: "ring_configuration",
    Resource.RING_BATTERY_LEVEL: "ring_battery_level",
    Resource.PERSONAL_INFO: "personal_info",
}

#: Guard against a malformed `next_token` loop. Oura pages in hundreds; a range
#: wide enough to need more than this is already refused by date validation.
MAX_PAGES = 100


class OuraApiClient:
    """Reads Oura user collections.

    Raises `httpx2.HTTPStatusError` on a non-2xx response and the usual
    `httpx2` transport errors otherwise. `OuraService` owns the translation of
    those into domain errors, so this stays a transport concern.
    """

    def __init__(self, http: AsyncClient, config: Config) -> None:
        self._http = http
        self._timeout = config.oura_request_timeout_seconds

    async def fetch_range(
        self,
        access_token: str,
        resource: Resource,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Every document for a resource between two ISO dates, all pages."""
        if resource is Resource.HEARTRATE:
            # A datetime-bounded time series rather than a daily collection.
            # Oura treats the bounds as instants, so widen to whole days.
            params = {
                "start_datetime": f"{start_date}T00:00:00+00:00",
                "end_datetime": f"{end_date}T23:59:59+00:00",
            }
        else:
            params = {"start_date": start_date, "end_date": end_date}

        return await self._paginate(access_token, RESOURCE_PATHS[resource], params)

    async def fetch_personal_info(self, access_token: str) -> dict[str, Any]:
        """The profile singleton, which takes no range and does not paginate."""
        response = await self._http.get(
            f"{API_URL}/v2/usercollection/personal_info",
            headers=self._headers(access_token),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    async def _paginate(
        self, access_token: str, segment: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        url = f"{API_URL}/v2/usercollection/{segment}"
        query = dict(params)
        records: list[dict[str, Any]] = []

        for _ in range(MAX_PAGES):
            response = await self._http.get(
                url,
                params=query,
                headers=self._headers(access_token),
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            records.extend(payload.get("data", []))

            next_token = payload.get("next_token")
            if not next_token:
                return records
            query["next_token"] = next_token

        return records

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}


class WebhookSubscriptionClient:
    """Manage Oura webhook subscriptions.

    These routes authenticate with the application's client id and secret
    rather than a user access token, and subscriptions expire -- hence `renew`.
    """

    def __init__(self, http: AsyncClient, config: Config) -> None:
        self._http = http
        self._headers = {
            "x-client-id": config.oura_client_id,
            "x-client-secret": config.oura_client_secret.get_secret_value(),
            "Content-Type": "application/json",
        }
        self._timeout = config.oura_request_timeout_seconds
        self._base = f"{API_URL}/v2/webhook/subscription"

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        response = await self._http.get(
            self._base, headers=self._headers, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()

    async def create_subscription(
        self,
        *,
        callback_url: str,
        verification_token: str,
        event_type: str,
        data_type: str,
    ) -> dict[str, Any]:
        response = await self._http.post(
            self._base,
            headers=self._headers,
            json={
                "callback_url": callback_url,
                "verification_token": verification_token,
                "event_type": event_type,
                "data_type": data_type,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    async def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        response = await self._http.put(
            f"{self._base}/renew/{subscription_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    async def delete_subscription(self, subscription_id: str) -> None:
        response = await self._http.delete(
            f"{self._base}/{subscription_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
