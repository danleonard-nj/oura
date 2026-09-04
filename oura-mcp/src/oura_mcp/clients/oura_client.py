"""Blocking Oura API client.

Everything in this module runs on a worker thread, never on the event loop.
`OuraService` is the only caller and it is responsible for the offload; keeping
the blocking code in its own module makes that boundary obvious.

`oura-ring` covers most of the v2 surface. Four collections that exist in Oura's
published OpenAPI spec are absent from the library, so they are issued here
through the same authenticated session rather than reimplementing the client:

    daily_resilience, daily_cardiovascular_age, vO2_max, ring_battery_level

Webhook subscription management is likewise absent and lives in
`WebhookSubscriptionClient` below.
"""

from __future__ import annotations

import threading
from typing import Any

import requests
from oura_ring import API_URL, OuraClient

from oura_mcp.models.resources import Resource

#: Collections `oura-ring` does not expose, mapped to their v2 path segment.
#: Each is a plain date-ranged, `next_token`-paginated user collection.
UNSUPPORTED_BY_LIBRARY: dict[Resource, str] = {
    Resource.DAILY_RESILIENCE: "daily_resilience",
    Resource.DAILY_CARDIOVASCULAR_AGE: "daily_cardiovascular_age",
    Resource.VO2_MAX: "vO2_max",
    Resource.RING_BATTERY_LEVEL: "ring_battery_level",
}

#: Resource -> the `oura-ring` method that serves it.
LIBRARY_METHODS: dict[Resource, str] = {
    Resource.DAILY_SLEEP: "get_daily_sleep",
    Resource.DAILY_READINESS: "get_daily_readiness",
    Resource.DAILY_ACTIVITY: "get_daily_activity",
    Resource.DAILY_STRESS: "get_daily_stress",
    Resource.DAILY_SPO2: "get_daily_spo2",
    Resource.SLEEP: "get_sleep_periods",
    Resource.SLEEP_TIME: "get_sleep_time",
    Resource.WORKOUT: "get_workouts",
    Resource.SESSION: "get_sessions",
    Resource.TAG: "get_tags",
    Resource.ENHANCED_TAG: "get_enhanced_tag",
    Resource.REST_MODE_PERIOD: "get_rest_mode_period",
    Resource.RING_CONFIGURATION: "get_ring_configuration",
}


class BlockingOuraClient:
    """Thread-confined access to the Oura API.

    `requests.Session` is not safe to share across threads while mutating its
    headers, and the access token changes on refresh. Each worker thread
    therefore gets its own `OuraClient`, and the bearer token is applied per
    call. Connections are still pooled within a thread.
    """

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        self._timeout = timeout_seconds
        self._local = threading.local()

    def _client(self, access_token: str) -> OuraClient:
        client: OuraClient | None = getattr(self._local, "client", None)
        if client is None:
            client = OuraClient(access_token)
            self._local.client = client
        else:
            client.session.headers["Authorization"] = f"Bearer {access_token}"
        return client

    # ── Collections served by oura-ring ───────────────────────────────────

    def fetch_range(
        self,
        access_token: str,
        resource: Resource,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch every document for `resource` between two ISO dates.

        Pagination is followed to completion, so one call covers the whole
        range rather than one request per day.
        """
        client = self._client(access_token)

        if resource is Resource.HEARTRATE:
            # Heart rate is a datetime-bounded time series. Oura treats the
            # bounds as inclusive instants, so widen to whole days.
            return client.get_heart_rate(
                start_datetime=f"{start_date}T00:00:00+00:00",
                end_datetime=f"{end_date}T23:59:59+00:00",
            )

        if resource in UNSUPPORTED_BY_LIBRARY:
            return self._paginate(
                client,
                UNSUPPORTED_BY_LIBRARY[resource],
                {"start_date": start_date, "end_date": end_date},
            )

        method_name = LIBRARY_METHODS.get(resource)
        if method_name is None:
            raise ValueError(f"{resource} is not a date-ranged collection")
        return getattr(client, method_name)(start_date=start_date, end_date=end_date)

    def fetch_personal_info(self, access_token: str) -> dict[str, Any]:
        return self._client(access_token).get_personal_info()

    # ── Collections issued directly ───────────────────────────────────────

    def _paginate(
        self, client: OuraClient, segment: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Walk `next_token` for a collection oura-ring does not implement."""
        url = f"{API_URL}/v2/usercollection/{segment}"
        query = dict(params)
        records: list[dict[str, Any]] = []

        while True:
            response = client.session.get(url, params=query, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
            records.extend(payload.get("data", []))

            next_token = payload.get("next_token")
            if not next_token:
                return records
            query["next_token"] = next_token

    def close(self) -> None:
        client: OuraClient | None = getattr(self._local, "client", None)
        if client is not None:
            client.close()
            self._local.client = None


class WebhookSubscriptionClient:
    """Manage Oura webhook subscriptions.

    Blocking; runs on a worker thread like the rest of this module. These routes
    authenticate with the application's client id and secret rather than a user
    access token, and are absent from `oura-ring`.
    """

    def __init__(self, client_id: str, client_secret: str, *, timeout_seconds: int = 30) -> None:
        self._headers = {
            "x-client-id": client_id,
            "x-client-secret": client_secret,
            "Content-Type": "application/json",
        }
        self._timeout = timeout_seconds
        self._base = f"{API_URL}/v2/webhook/subscription"

    def list_subscriptions(self) -> list[dict[str, Any]]:
        response = requests.get(self._base, headers=self._headers, timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def create_subscription(
        self,
        *,
        callback_url: str,
        verification_token: str,
        event_type: str,
        data_type: str,
    ) -> dict[str, Any]:
        response = requests.post(
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

    def delete_subscription(self, subscription_id: str) -> None:
        response = requests.delete(
            f"{self._base}/{subscription_id}", headers=self._headers, timeout=self._timeout
        )
        response.raise_for_status()

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        response = requests.put(
            f"{self._base}/renew/{subscription_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()
