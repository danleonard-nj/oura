"""The Oura HTTP boundary, exercised against a mock transport.

No network, no `oura-ring`: these assert the requests we actually put on the
wire -- paths, query parameters, pagination and the bearer token on every page.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from app.clients.oura_client import (
    MAX_PAGES,
    RESOURCE_PATHS,
    OuraApiClient,
    WebhookSubscriptionClient,
)
from app.models.resources import Resource

RANGED = [r for r in Resource if r is not Resource.PERSONAL_INFO]


def build(handler, config) -> tuple[OuraApiClient, list[httpx2.Request]]:
    """An OuraApiClient wired to a mock transport, plus the recorded requests."""
    sent: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return handler(request, len(sent))

    client = OuraApiClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(record)), config.oura
    )
    return client, sent


def page(_request, count):
    """Two pages, then stop."""
    return httpx2.Response(
        200,
        json={
            "data": [{"day": "2026-09-01"}],
            "next_token": "page-2" if count % 2 else None,
        },
    )


# ── Paths and parameters ──────────────────────────────────────────────────


@pytest.mark.parametrize("resource", RANGED, ids=lambda r: r.value)
async def test_every_resource_hits_its_documented_path(resource, config):
    client, sent = build(lambda r, n: httpx2.Response(200, json={"data": []}), config)

    await client.fetch_range("tok", resource, "2026-09-01", "2026-09-02")

    assert urlsplit(str(sent[0].url)).path == (
        f"/v2/usercollection/{RESOURCE_PATHS[resource]}"
    )


def test_every_resource_has_a_path():
    """A resource without a path entry would KeyError at request time."""
    assert set(RESOURCE_PATHS) == set(Resource)


async def test_vo2_max_keeps_ouras_odd_capitalisation(config):
    """The collection path is `vO2_max` even though the webhook type is `vo2_max`."""
    client, sent = build(lambda r, n: httpx2.Response(200, json={"data": []}), config)

    await client.fetch_range("tok", Resource.VO2_MAX, "2026-09-01", "2026-09-02")

    assert urlsplit(str(sent[0].url)).path.endswith("/vO2_max")


async def test_daily_collections_send_date_bounds(config):
    client, sent = build(lambda r, n: httpx2.Response(200, json={"data": []}), config)

    await client.fetch_range("tok", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-03")

    query = parse_qs(urlsplit(str(sent[0].url)).query)
    assert query["start_date"] == ["2026-09-01"]
    assert query["end_date"] == ["2026-09-03"]


async def test_heart_rate_sends_datetime_bounds_covering_whole_days(config):
    """Oura treats heart-rate bounds as instants, so they must span the day."""
    client, sent = build(lambda r, n: httpx2.Response(200, json={"data": []}), config)

    await client.fetch_range("tok", Resource.HEARTRATE, "2026-09-01", "2026-09-01")

    query = parse_qs(urlsplit(str(sent[0].url)).query)
    assert query["start_datetime"] == ["2026-09-01T00:00:00+00:00"]
    assert query["end_datetime"] == ["2026-09-01T23:59:59+00:00"]
    assert "start_date" not in query


async def test_personal_info_takes_no_range_and_returns_the_document(config):
    client, sent = build(lambda r, n: httpx2.Response(200, json={"age": 41}), config)

    result = await client.fetch_personal_info("tok")

    assert result == {"age": 41}
    assert urlsplit(str(sent[0].url)).query == ""


# ── Pagination and auth ───────────────────────────────────────────────────


async def test_pagination_follows_next_token(config):
    client, sent = build(page, config)

    records = await client.fetch_range("tok", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02")

    assert len(records) == 2
    assert parse_qs(urlsplit(str(sent[1].url)).query)["next_token"] == ["page-2"]


async def test_the_current_token_is_sent_on_every_page(config):
    """A refresh between calls must reach the wire, including page two."""
    client, sent = build(page, config)

    for token in ("old-token", "refreshed-token"):
        await client.fetch_range("tok-ignored", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02") \
            if False else await client.fetch_range(
                token, Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02"
            )

    assert [r.headers["Authorization"] for r in sent] == [
        "Bearer old-token",
        "Bearer old-token",
        "Bearer refreshed-token",
        "Bearer refreshed-token",
    ]


async def test_pagination_is_bounded(config):
    """A server that always returns next_token must not loop forever."""
    client, sent = build(
        lambda r, n: httpx2.Response(200, json={"data": [{"day": "x"}], "next_token": "more"}),
        config,
    )

    records = await client.fetch_range("tok", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02")

    assert len(sent) == MAX_PAGES
    assert len(records) == MAX_PAGES


async def test_a_missing_data_key_is_tolerated(config):
    client, _ = build(lambda r, n: httpx2.Response(200, json={}), config)

    assert await client.fetch_range("tok", Resource.TAG, "2026-09-01", "2026-09-02") == []


# ── Errors ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_non_2xx_raises_for_the_service_to_translate(status, config):
    client, _ = build(lambda r, n: httpx2.Response(status, json={"detail": "no"}), config)

    with pytest.raises(httpx2.HTTPStatusError) as excinfo:
        await client.fetch_range("tok", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02")

    assert excinfo.value.response.status_code == status


async def test_a_mid_pagination_failure_propagates(config):
    def handler(request, count):
        if count == 1:
            return httpx2.Response(200, json={"data": [{"day": "1"}], "next_token": "p2"})
        return httpx2.Response(500, json={})

    client, _ = build(handler, config)

    with pytest.raises(httpx2.HTTPStatusError):
        await client.fetch_range("tok", Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02")


# ── Webhook subscriptions ─────────────────────────────────────────────────


def build_subscriptions(handler, config):
    sent: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return handler(request)

    client = WebhookSubscriptionClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(record)), config.oura
    )
    return client, sent


async def test_subscription_calls_authenticate_with_client_credentials(config):
    client, sent = build_subscriptions(lambda r: httpx2.Response(200, json=[]), config)

    await client.list_subscriptions()

    assert sent[0].headers["x-client-id"] == config.oura.client_id
    assert sent[0].headers["x-client-secret"] == config.oura.client_secret.get_secret_value()
    assert "Authorization" not in sent[0].headers


async def test_creating_a_subscription_posts_the_expected_body(config):
    import json

    client, sent = build_subscriptions(
        lambda r: httpx2.Response(200, json={"id": "sub-1"}), config
    )

    await client.create_subscription(
        callback_url="https://oura.test/webhooks/oura",
        verification_token="vt",
        event_type="update",
        data_type="daily_sleep",
    )

    body = json.loads(sent[0].content)
    assert body == {
        "callback_url": "https://oura.test/webhooks/oura",
        "verification_token": "vt",
        "event_type": "update",
        "data_type": "daily_sleep",
    }
    assert sent[0].method == "POST"


async def test_renew_and_delete_target_the_subscription_id(config):
    client, sent = build_subscriptions(lambda r: httpx2.Response(200, json={}), config)

    await client.renew_subscription("sub-9")
    await client.delete_subscription("sub-9")

    assert sent[0].method == "PUT"
    assert urlsplit(str(sent[0].url)).path.endswith("/renew/sub-9")
    assert sent[1].method == "DELETE"
    assert urlsplit(str(sent[1].url)).path.endswith("/subscription/sub-9")
