"""End-to-end tests over the real ASGI application.

The app is built by `create_app`, so these exercise the actual composition:
Starlette root, the MCP Streamable HTTP transport attached with `Route`, the
bearer-token middleware, and the session manager started from the root lifespan.
Only the outermost infrastructure -- Mongo, Redis and the blocking Oura client
-- is swapped for fakes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from oura_mcp.clients.oura_client import BlockingOuraClient, WebhookSubscriptionClient
from oura_mcp.config import Config
from oura_mcp.models.resources import Resource
from oura_mcp.repositories.audit_repository import AuditRepository
from oura_mcp.repositories.token_repository import TokenRepository
from oura_mcp.repositories.webhook_repository import WebhookRepository
from tests.conftest import (
    FakeAuditRepository,
    FakeRedis,
    FakeTokenRepository,
    FakeWebhookRepository,
    make_token,
)

HOST = "oura.test"
API_KEY = "test-mcp-api-key"

SLEEP_RECORDS = [
    {"day": "2026-09-01", "score": 82, "id": "s1"},
    {"day": "2026-09-02", "score": 74, "id": "s2"},
]


class StubOuraClient:
    """Stands in for the blocking client; records what was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def fetch_range(self, token, resource, start, end):
        self.calls.append((resource, start, end))
        if self.error:
            raise self.error
        if resource is Resource.WORKOUT:
            return [{"day": "2026-09-01", "activity": "cycling"}]
        return SLEEP_RECORDS

    def fetch_personal_info(self, token):
        self.calls.append(("personal_info",))
        if self.error:
            raise self.error
        return {"age": 41, "biological_sex": "male", "weight": 78.0}

    def fetch_document(self, token, resource, document_id):  # pragma: no cover
        return {"id": document_id}


class StubSubscriptions:
    """Stands in for Oura's webhook subscription routes."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.renewed: list[str] = []
        self.deleted: list[str] = []

    def list_subscriptions(self):
        return [
            {
                "id": "sub-1",
                "data_type": "daily_sleep",
                "expiration_time": "2026-12-01T00:00:00+00:00",
            }
        ]

    def create_subscription(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "sub-2", **kwargs}

    def renew_subscription(self, subscription_id):
        self.renewed.append(subscription_id)
        return {"id": subscription_id, "expiration_time": "2027-01-01T00:00:00+00:00"}

    def delete_subscription(self, subscription_id):
        self.deleted.append(subscription_id)
        return None


@pytest.fixture
def stub_client() -> StubOuraClient:
    return StubOuraClient()


class FakeMongoClient:
    """Only `admin.command("ping")` is used, by the readiness probe."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.admin = self

    async def command(self, _name: str):
        if not self.available:
            raise RuntimeError("mongo is down")
        return {"ok": 1}

    async def close(self) -> None:
        return None


@pytest.fixture
def fakes(stub_client):
    return {
        "mongo": FakeMongoClient(),
        "redis": FakeRedis(),
        "tokens": FakeTokenRepository(make_token()),
        "audit": FakeAuditRepository(),
        "webhooks": FakeWebhookRepository(),
        "oura": stub_client,
        "subscriptions": StubSubscriptions(),
    }


@pytest.fixture
def client(config: Config, fakes, monkeypatch):
    """A TestClient over the real app, with infrastructure swapped at the container.

    Overriding registrations rather than patching module globals keeps the
    composition honest: if a service stops taking its dependency through the
    constructor, this stops working.
    """
    from pymongo import AsyncMongoClient
    from pymongo.asynchronous.database import AsyncDatabase
    from redis.asyncio import Redis

    import oura_mcp.app as app_module
    from oura_mcp.container import build_services

    def build_provider_with_fakes(cfg: Config):
        services = build_services(cfg)
        services.add_singleton(AsyncMongoClient, factory=lambda _: fakes["mongo"])
        services.add_singleton(AsyncDatabase, factory=lambda _: object())
        services.add_singleton(Redis, factory=lambda _: fakes["redis"])
        services.add_singleton(TokenRepository, factory=lambda _: fakes["tokens"])
        services.add_singleton(AuditRepository, factory=lambda _: fakes["audit"])
        services.add_singleton(WebhookRepository, factory=lambda _: fakes["webhooks"])
        services.add_singleton(BlockingOuraClient, factory=lambda _: fakes["oura"])
        services.add_singleton(
            WebhookSubscriptionClient, factory=lambda _: fakes["subscriptions"]
        )
        return services.build_provider(eager_all=False)

    monkeypatch.setattr(app_module, "build_provider", build_provider_with_fakes)
    monkeypatch.setattr(app_module, "ensure_indexes", _noop)

    with TestClient(app_module.create_app(config)) as test_client:
        yield test_client


async def _noop(*_args, **_kwargs) -> None:
    return None


def rpc(client: TestClient, method: str, params: dict | None = None, request_id: int = 1):
    """Send one JSON-RPC call and unwrap the SSE frame the transport returns."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Host": HOST,
        },
    )
    assert response.status_code == 200, response.text
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no data frame in response: {response.text!r}")


def initialize(client: TestClient) -> None:
    rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
        },
    )


def call_tool(client: TestClient, name: str, arguments: dict | None = None) -> dict:
    initialize(client)
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, request_id=2)


# ── Health ────────────────────────────────────────────────────────────────


def test_health_is_ok_and_needs_no_dependencies(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_stays_ok_when_redis_is_down(client, fakes):
    fakes["redis"].available = False

    assert client.get("/health").status_code == 200


def test_health_requires_no_authentication(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)


def test_readiness_reports_dependency_state(client):
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {
        "configuration": True,
        "application": True,
        "mongo": True,
        "redis": True,
    }


def test_readiness_fails_when_redis_is_unavailable(client, fakes):
    fakes["redis"].available = False

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] is False


def test_every_response_carries_a_correlation_id(client):
    response = client.get("/health")

    assert response.headers["x-correlation-id"]


def test_an_inbound_correlation_id_is_echoed(client):
    response = client.get("/health", headers={"x-correlation-id": "caller-supplied"})

    assert response.headers["x-correlation-id"] == "caller-supplied"


# ── MCP authentication ────────────────────────────────────────────────────


def test_mcp_rejects_a_request_with_no_token(client):
    response = client.post("/mcp", json={}, headers={"Host": HOST})

    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong-key", "Basic dGVzdDp0ZXN0", "test-mcp-api-key", "Bearer ", ""],
)
def test_mcp_rejects_malformed_or_wrong_credentials(client, header):
    response = client.post("/mcp", json={}, headers={"Authorization": header, "Host": HOST})

    assert response.status_code == 401


def test_mcp_accepts_the_configured_key(client):
    initialize(client)  # asserts a 200 internally


def test_mcp_endpoint_is_reachable_without_a_trailing_slash_redirect(client):
    """A Mount would 307 here; the Route composition must not."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json, text/event-stream",
            "Host": HOST,
        },
        follow_redirects=False,
    )

    assert response.status_code != 307, "the MCP endpoint redirected; clients will not follow"


def test_transport_security_rejects_an_unexpected_host(client):
    """DNS-rebinding protection must be live, not left on the localhost default."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json, text/event-stream",
            "Host": "attacker.example.com",
        },
    )

    assert response.status_code == 421


# ── MCP tools ─────────────────────────────────────────────────────────────


def test_the_expected_tools_are_advertised(client):
    initialize(client)
    tools = {t["name"] for t in rpc(client, "tools/list", request_id=2)["result"]["tools"]}

    assert {
        "oura_summary", "oura_sleep", "oura_readiness", "oura_activity", "oura_stress",
        "oura_heart_rate", "oura_workouts", "oura_sessions", "oura_spo2", "oura_resilience",
        "oura_cardiovascular_age", "oura_vo2_max", "oura_tags", "oura_ring_info",
        "oura_personal_info", "oura_query",
    } <= tools


def test_every_tool_has_a_description_and_typed_parameters(client):
    initialize(client)
    tools = rpc(client, "tools/list", request_id=2)["result"]["tools"]

    for tool in tools:
        assert len(tool.get("description", "")) > 60, f"{tool['name']} is under-described"
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        for name, prop in schema.get("properties", {}).items():
            assert "type" in prop or "anyOf" in prop, f"{tool['name']}.{name} is untyped"


def test_a_tool_call_returns_structured_content(client, fakes):
    result = call_tool(client, "oura_sleep", {"start_date": "2026-09-01", "end_date": "2026-09-02"})

    structured = result["result"]["structuredContent"]
    assert structured["resource"] == "daily_sleep"
    assert structured["count"] == 2
    assert structured["records"] == SLEEP_RECORDS
    assert structured["cache"] == "miss"
    assert fakes["oura"].calls == [(Resource.DAILY_SLEEP, "2026-09-01", "2026-09-02")]


def test_a_tool_call_with_no_dates_uses_a_sensible_default(client, fakes):
    call_tool(client, "oura_sleep")

    resource, start, end = fakes["oura"].calls[0]
    assert resource is Resource.DAILY_SLEEP
    # Defaults are computed in UTC so a container's local timezone cannot change
    # which day a tool call resolves to.
    assert end == datetime.now(UTC).date().isoformat()
    assert start < end


def test_personal_info_returns_a_single_document(client):
    result = call_tool(client, "oura_personal_info")

    assert result["result"]["structuredContent"]["document"]["age"] == 41


def test_summary_joins_resources_by_day(client):
    result = call_tool(
        client, "oura_summary", {"start_date": "2026-09-01", "end_date": "2026-09-02"}
    )

    summary = result["result"]["structuredContent"]
    days = {d["day"]: d for d in summary["days"]}
    assert set(days) == {"2026-09-01", "2026-09-02"}
    assert days["2026-09-01"]["sleep"]["score"] == 82
    assert days["2026-09-01"]["workouts"][0]["activity"] == "cycling"


def test_an_invalid_date_is_reported_as_a_tool_error(client):
    result = call_tool(client, "oura_sleep", {"start_date": "not-a-date"})

    assert result["result"]["isError"] is True
    assert "ISO-8601" in json.dumps(result["result"]["content"])


def test_an_oversized_range_is_refused(client, fakes):
    result = call_tool(
        client, "oura_heart_rate", {"start_date": "2026-01-01", "end_date": "2026-06-01"}
    )

    assert result["result"]["isError"] is True
    assert "14-day maximum" in json.dumps(result["result"]["content"])
    assert fakes["oura"].calls == [], "an over-large range still reached Oura"


def test_an_upstream_403_becomes_a_readable_tool_error(client, fakes):
    from tests.conftest import http_error

    fakes["oura"].error = http_error(403)

    result = call_tool(client, "oura_sleep", {"start_date": "2026-09-01"})

    content = json.dumps(result["result"]["content"])
    assert result["result"]["isError"] is True
    assert "scope" in content
    assert "access-0" not in content, "a token leaked into a tool error"


def test_oura_query_rejects_an_unknown_resource(client):
    result = call_tool(client, "oura_query", {"resource": "made_up"})

    assert result["result"]["isError"] is True
    assert "Unknown resource" in json.dumps(result["result"]["content"])


def test_oura_query_reaches_a_collection_without_a_named_tool(client, fakes):
    call_tool(client, "oura_query", {"resource": "sleep_time", "start_date": "2026-09-01"})

    assert fakes["oura"].calls[0][0] is Resource.SLEEP_TIME


def test_a_tool_call_is_audited(client, fakes):
    call_tool(client, "oura_sleep", {"start_date": "2026-09-01", "end_date": "2026-09-02"})

    actions = [e.action.value for e in fakes["audit"].events]
    assert "tool_invoked" in actions
    invocation = next(e for e in fakes["audit"].events if e.action.value == "tool_invoked")
    assert invocation.tool == "oura_sleep"
    assert invocation.resource == "daily_sleep"
    assert invocation.start_date == "2026-09-01"
    assert invocation.duration_ms is not None
    assert invocation.correlation_id


def test_a_mongo_audit_failure_does_not_fail_the_tool(client, fakes):
    fakes["audit"].failing = True

    result = call_tool(client, "oura_sleep", {"start_date": "2026-09-01"})

    assert "isError" not in result["result"] or result["result"]["isError"] is False
    assert result["result"]["structuredContent"]["count"] == 2


def test_no_audit_record_contains_token_material(client, fakes):
    call_tool(client, "oura_sleep", {"start_date": "2026-09-01"})

    dumped = json.dumps([e.model_dump(mode="json") for e in fakes["audit"].events])
    assert "access-0" not in dumped
    assert "refresh-0" not in dumped


# ── OAuth ─────────────────────────────────────────────────────────────────


def test_authorize_redirects_to_oura(client):
    response = client.get("/oauth/authorize", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://cloud.ouraring.com/oauth/authorize")
    assert "state=" in location
    assert "client_secret" not in location


def test_callback_rejects_an_unknown_state(client):
    response = client.get("/oauth/callback?code=abc&state=never-issued")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_state"


def test_callback_without_a_code_is_refused(client):
    response = client.get("/oauth/callback?state=x")

    assert response.status_code == 400


def test_a_denied_authorization_is_reported(client):
    response = client.get("/oauth/callback?error=access_denied&state=x")

    assert response.status_code == 400
    assert response.json()["error"] == "oauth_denied"


# ── Webhooks ──────────────────────────────────────────────────────────────


def test_webhook_challenge_is_echoed(client):
    response = client.get(
        "/webhooks/oura",
        params={"verification_token": "test-verification-token", "challenge": "nonce-9"},
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "nonce-9"}


def test_webhook_challenge_with_a_wrong_token_is_refused(client):
    response = client.get(
        "/webhooks/oura", params={"verification_token": "nope", "challenge": "nonce-9"}
    )

    assert response.status_code == 401


def test_a_correctly_signed_event_is_accepted(client, config, fakes):
    body = json.dumps(
        {"event_type": "update", "data_type": "daily_sleep", "object_id": "o1"},
        separators=(",", ":"),
    ).encode()
    timestamp = "1767225600"
    signature = hmac.new(
        config.oura_client_secret.get_secret_value().encode(),
        timestamp.encode() + body,
        hashlib.sha256,
    ).hexdigest().upper()

    response = client.post(
        "/webhooks/oura",
        content=body,
        headers={
            "x-oura-signature": signature,
            "x-oura-timestamp": timestamp,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert len(fakes["webhooks"].records) == 1


def test_an_unsigned_event_is_rejected(client):
    response = client.post("/webhooks/oura", json={"event_type": "update"})

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_signature"


def test_a_forged_signature_is_rejected(client, fakes):
    response = client.post(
        "/webhooks/oura",
        content=b'{"event_type":"update","data_type":"sleep","object_id":"x"}',
        headers={"x-oura-signature": "DEADBEEF", "x-oura-timestamp": "1767225600"},
    )

    assert response.status_code == 401
    assert fakes["webhooks"].records == []


# ── Admin API ─────────────────────────────────────────────────────────────


def test_admin_status_requires_the_api_key(client):
    assert client.get("/api/status").status_code == 401


def test_admin_status_never_returns_token_material(client):
    response = client.get("/api/status", headers={"Authorization": f"Bearer {API_KEY}"})

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["scopes"] == ["daily", "personal"]
    serialised = json.dumps(body)
    assert "access-0" not in serialised
    assert "refresh-0" not in serialised
    assert "access_token" not in serialised


def test_admin_status_reports_a_disconnected_account(client, fakes):
    fakes["tokens"].token = None

    response = client.get("/api/status", headers={"Authorization": f"Bearer {API_KEY}"})

    assert response.json()["connected"] is False


def test_admin_audit_requires_the_api_key(client):
    assert client.get("/api/audit").status_code == 401
    assert client.get("/api/audit", headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200


# ── Webhook subscription management ───────────────────────────────────────


@pytest.fixture
def subscription_client(fakes):
    """The stub registered in place of the Oura subscription client."""
    return fakes["subscriptions"]


AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/webhooks/subscriptions"),
        ("POST", "/api/webhooks/subscriptions"),
        ("PUT", "/api/webhooks/subscriptions/sub-1/renew"),
        ("DELETE", "/api/webhooks/subscriptions/sub-1"),
    ],
)
def test_subscription_routes_require_the_api_key(client, method, path):
    assert client.request(method, path).status_code == 401


def test_subscriptions_can_be_listed(client, subscription_client):
    response = client.get("/api/webhooks/subscriptions", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["subscriptions"][0]["expiration_time"]


def test_a_subscription_can_be_created(client, subscription_client, config):
    response = client.post(
        "/api/webhooks/subscriptions", headers=AUTH, json={"data_type": "daily_sleep"}
    )

    assert response.status_code == 200
    created = subscription_client.created[0]
    # callback_url defaults to the configured public host.
    assert created["callback_url"] == f"https://{config.public_host}/webhooks/oura"
    assert created["event_type"] == "update"
    assert created["verification_token"] == "test-verification-token"


def test_creating_a_subscription_requires_a_data_type(client, subscription_client):
    response = client.post("/api/webhooks/subscriptions", headers=AUTH, json={})

    assert response.status_code == 400
    assert subscription_client.created == []


def test_a_subscription_can_be_renewed(client, subscription_client):
    response = client.put("/api/webhooks/subscriptions/sub-1/renew", headers=AUTH)

    assert response.status_code == 200
    assert subscription_client.renewed == ["sub-1"]


def test_a_subscription_can_be_deleted(client, subscription_client):
    response = client.delete("/api/webhooks/subscriptions/sub-1", headers=AUTH)

    assert response.status_code == 200
    assert subscription_client.deleted == ["sub-1"]


def test_an_upstream_subscription_failure_leaks_nothing(client, subscription_client, config):
    from tests.conftest import http_error

    def boom(**_kwargs):
        raise http_error(403)

    subscription_client.create_subscription = boom

    response = client.post(
        "/api/webhooks/subscriptions", headers=AUTH, json={"data_type": "sleep"}
    )

    assert response.status_code == 502
    body = json.dumps(response.json())
    assert config.oura_client_secret.get_secret_value() not in body
    assert "test-verification-token" not in body
