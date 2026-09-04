"""Health probes and administrative endpoints."""

from __future__ import annotations

from pymongo import AsyncMongoClient
from redis.asyncio import Redis
from requests import HTTPError, RequestException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from oura_mcp.api.dependencies import admin_guard, resolve
from oura_mcp.clients.oura_client import WebhookSubscriptionClient
from oura_mcp.config import Config
from oura_mcp.infrastructure import mongo, redis as redis_infra
from oura_mcp.infrastructure.executor import ThreadOffloader
from oura_mcp.infrastructure.logging import get_logger
from oura_mcp.repositories.audit_repository import AuditRepository
from oura_mcp.repositories.webhook_repository import WebhookRepository
from oura_mcp.services.token_service import TokenService

logger = get_logger(__name__)


async def health(_: Request) -> JSONResponse:
    """Liveness. Deliberately dependency-free.

    Kubernetes restarts the pod when this fails, so it must not go red because
    Mongo, Redis or Oura is having a bad day -- restarting would not help.
    """
    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> JSONResponse:
    """Readiness. Checks the dependencies needed to serve a request.

    Mongo holds the OAuth token and Redis guards its refresh, so without either
    the service cannot safely answer an MCP call. Oura itself is never called
    here: a probe every few seconds would burn rate limit for no benefit.
    """
    provider = getattr(request.app.state, "provider", None)
    if provider is None:
        return JSONResponse(
            {"status": "not_ready", "checks": {"application": False}}, status_code=503
        )

    mongo_ok = await mongo.ping(resolve(request, AsyncMongoClient))
    redis_ok = await redis_infra.ping(resolve(request, Redis))

    checks = {
        "configuration": True,
        "application": True,
        "mongo": mongo_ok,
        "redis": redis_ok,
    }
    ready_now = all(checks.values())
    return JSONResponse(
        {"status": "ready" if ready_now else "not_ready", "checks": checks},
        status_code=200 if ready_now else 503,
    )


async def status(request: Request) -> JSONResponse:
    """Whether an Oura account is connected, and with which scopes.

    Reports token metadata only. The tokens themselves are never returned by
    any endpoint or MCP tool.
    """
    if (denied := admin_guard(request)) is not None:
        return denied

    config = resolve(request, Config)
    tokens = resolve(request, TokenService)
    connection = await tokens.status(config.oura_account_id)

    return JSONResponse(
        {
            "account_id": connection.account_id,
            "connected": connection.connected,
            "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
            "scopes": connection.scopes,
            "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
            "token_version": connection.version,
            "mcp_path": config.mcp_path,
            "mcp_stateless": config.mcp_stateless,
            "cache_enabled": config.cache_enabled,
        }
    )


async def audit(request: Request) -> JSONResponse:
    """Recent audit events for the configured account."""
    if (denied := admin_guard(request)) is not None:
        return denied

    config = resolve(request, Config)
    repository = resolve(request, AuditRepository)
    limit = _int_param(request, "limit", default=50, maximum=500)
    events = await repository.recent(config.oura_account_id, limit=limit)
    return JSONResponse({"count": len(events), "events": events}, status_code=200)


async def webhook_events(request: Request) -> JSONResponse:
    """Recently received Oura webhook notifications."""
    if (denied := admin_guard(request)) is not None:
        return denied

    repository = resolve(request, WebhookRepository)
    limit = _int_param(request, "limit", default=50, maximum=500)
    events = await repository.recent(limit=limit)
    return JSONResponse({"count": len(events), "events": events}, status_code=200)


async def list_subscriptions(request: Request) -> JSONResponse:
    """List this application's Oura webhook subscriptions.

    Each carries an `expiration_time`: Oura subscriptions expire and have to be
    renewed, so this is worth checking periodically.
    """
    if (denied := admin_guard(request)) is not None:
        return denied

    return await _subscription_call(
        request, lambda client: client.list_subscriptions(), key="subscriptions"
    )


async def create_subscription(request: Request) -> JSONResponse:
    """Register a webhook subscription with Oura.

    Administrative, not part of the MCP surface: this writes to Oura's
    subscription resource, never to health data. One subscription is needed per
    event_type/data_type pair, and Oura verifies the callback URL by calling
    GET /webhooks/oura before activating it, so the service must already be
    reachable at its public hostname.
    """
    if (denied := admin_guard(request)) is not None:
        return denied

    config = resolve(request, Config)
    if config.oura_webhook_verification_token is None:
        return JSONResponse(
            {
                "error": "not_configured",
                "detail": (
                    "OURA_WEBHOOK_VERIFICATION_TOKEN is not set, so the verification "
                    "callback would fail and Oura would refuse the subscription."
                ),
            },
            status_code=409,
        )

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    data_type = body.get("data_type")
    event_type = body.get("event_type", "update")
    if not data_type:
        return JSONResponse(
            {"error": "invalid_payload", "detail": "data_type is required."}, status_code=400
        )

    callback_url = body.get("callback_url") or _default_callback_url(config)
    if not callback_url:
        return JSONResponse(
            {
                "error": "invalid_payload",
                "detail": "callback_url is required when PUBLIC_HOST is not configured.",
            },
            status_code=400,
        )

    return await _subscription_call(
        request,
        lambda client: client.create_subscription(
            callback_url=callback_url,
            verification_token=config.oura_webhook_verification_token.get_secret_value(),
            event_type=event_type,
            data_type=data_type,
        ),
        key="subscription",
    )


async def renew_subscription(request: Request) -> JSONResponse:
    """Extend a subscription's expiration_time before Oura drops it."""
    if (denied := admin_guard(request)) is not None:
        return denied

    subscription_id = request.path_params["subscription_id"]
    return await _subscription_call(
        request,
        lambda client: client.renew_subscription(subscription_id),
        key="subscription",
    )


async def delete_subscription(request: Request) -> JSONResponse:
    """Cancel a subscription."""
    if (denied := admin_guard(request)) is not None:
        return denied

    subscription_id = request.path_params["subscription_id"]
    return await _subscription_call(
        request,
        lambda client: client.delete_subscription(subscription_id),
        key="deleted",
    )


async def _subscription_call(request: Request, operation, *, key: str) -> JSONResponse:
    """Run one blocking subscription call off the event loop and shape the result.

    Failures report only the upstream status: the request headers carry the
    client secret, so nothing derived from them may reach the response.
    """
    client = resolve(request, WebhookSubscriptionClient)
    offloader = resolve(request, ThreadOffloader)
    try:
        result = await offloader.run(operation, client)
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logger.warning("oura subscription call failed", extra={"upstream_status": status})
        return JSONResponse(
            {
                "error": "upstream_error",
                "detail": f"Oura rejected the subscription request (HTTP {status}).",
            },
            status_code=502,
        )
    except RequestException as exc:
        logger.warning("could not reach oura", extra={"error": type(exc).__name__})
        return JSONResponse(
            {"error": "upstream_error", "detail": "Could not reach the Oura API."},
            status_code=502,
        )
    return JSONResponse({key: result if result is not None else True})


def _default_callback_url(config: Config) -> str | None:
    return f"https://{config.public_host}/webhooks/oura" if config.public_host else None


def _int_param(request: Request, name: str, *, default: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError:
        return default


ROUTES = [
    Route("/health", health, methods=["GET"]),
    Route("/ready", ready, methods=["GET"]),
    Route("/api/status", status, methods=["GET"]),
    Route("/api/audit", audit, methods=["GET"]),
    Route("/api/webhooks/events", webhook_events, methods=["GET"]),
    Route("/api/webhooks/subscriptions", list_subscriptions, methods=["GET"]),
    Route("/api/webhooks/subscriptions", create_subscription, methods=["POST"]),
    Route("/api/webhooks/subscriptions/{subscription_id}/renew", renew_subscription, methods=["PUT"]),
    Route("/api/webhooks/subscriptions/{subscription_id}", delete_subscription, methods=["DELETE"]),
]
