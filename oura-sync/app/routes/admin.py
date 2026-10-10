"""Administrative endpoints: connection status, audit, request log, webhooks.

Every route here is behind `mcp.api_key`.
"""

from __future__ import annotations

import httpx2
from depi_quart import QuartInjector
from quart import Blueprint, request

from app.clients.oura_client import WebhookSubscriptionClient
from app.models.config import (
    CacheConfig,
    McpConfig,
    OuraConfig,
    ServiceConfig,
    SyncConfig,
    WebhookConfig,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.request_log_repository import RequestLogRepository
from app.repositories.webhook_repository import WebhookRepository
from app.services.token_service import TokenService
from app.utilities.auth import require_api_key
from app.utilities.http import int_arg, json_response
from app.utilities.logging import get_logger

logger = get_logger(__name__)


def create_admin_blueprint(injector: QuartInjector) -> Blueprint:
    admin_bp = Blueprint("admin", __name__, url_prefix="/api")
    guard = require_api_key(injector.provider.resolve(McpConfig))

    @admin_bp.route("/status", methods=["GET"])
    @guard
    @injector.inject
    async def status(
        tokens: TokenService,
        oura: OuraConfig,
        mcp: McpConfig,
        cache: CacheConfig,
        sync: SyncConfig,
    ):
        """Whether an Oura account is connected, and with which scopes.

        Reports token metadata only. The tokens themselves are never returned by
        any endpoint or MCP tool.
        """
        connection = await tokens.status(oura.account_id)

        return json_response(
            {
                "account_id": connection.account_id,
                "connected": connection.connected,
                "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
                "scopes": connection.scopes,
                "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
                "token_version": connection.version,
                "mcp_path": mcp.path,
                "mcp_stateless": mcp.stateless,
                "cache_enabled": cache.enabled,
                "sync_resources": [r.value for r in sync.resources],
            }
        )

    @admin_bp.route("/audit", methods=["GET"])
    @guard
    @injector.inject
    async def audit(repository: AuditRepository, oura: OuraConfig):
        """Recent audit events for the configured account."""
        limit = int_arg("limit", default=50, maximum=500)
        events = await repository.recent(oura.account_id, limit=limit)
        return json_response({"count": len(events), "events": events})

    @admin_bp.route("/requests", methods=["GET"])
    @guard
    @injector.inject
    async def request_log(repository: RequestLogRepository):
        """Recent HTTP requests, most-recent first.

        Everything that reached the app -- MCP calls, health probes excluded,
        and the opportunistic scanner traffic every public ingress attracts.
        Short retention (see `ensure_indexes`); this is for spotting patterns,
        not a durable audit trail.
        """
        limit = int_arg("limit", default=50, maximum=500)
        entries = await repository.recent(limit=limit)
        return json_response({"count": len(entries), "requests": entries})

    @admin_bp.route("/webhooks/events", methods=["GET"])
    @guard
    @injector.inject
    async def webhook_events(repository: WebhookRepository):
        """Recently received Oura webhook notifications."""
        limit = int_arg("limit", default=50, maximum=500)
        events = await repository.recent(limit=limit)
        return json_response({"count": len(events), "events": events})

    @admin_bp.route("/webhooks/subscriptions", methods=["GET"])
    @guard
    @injector.inject
    async def list_subscriptions(client: WebhookSubscriptionClient):
        """List this application's Oura webhook subscriptions.

        Each carries an `expiration_time`: Oura subscriptions expire and have to be
        renewed, so this is worth checking periodically.
        """
        return await _subscription_call(client.list_subscriptions, key="subscriptions")

    @admin_bp.route("/webhooks/subscriptions", methods=["POST"])
    @guard
    @injector.inject
    async def create_subscription(
        client: WebhookSubscriptionClient,
        webhooks: WebhookConfig,
        service: ServiceConfig,
    ):
        """Register a webhook subscription with Oura.

        Administrative, not part of the MCP surface: this writes to Oura's
        subscription resource, never to health data. One subscription is needed per
        event_type/data_type pair, and Oura verifies the callback URL by calling
        GET /webhooks/oura before activating it, so the service must already be
        reachable at its public hostname.
        """
        if webhooks.verification_token is None:
            return json_response(
                {
                    "error": "not_configured",
                    "detail": (
                        "webhooks.verification_token is not set, so the verification "
                        "callback would fail and Oura would refuse the subscription."
                    ),
                },
                409,
            )

        body = await request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return json_response({"error": "invalid_payload"}, 400)

        data_type = body.get("data_type")
        event_type = body.get("event_type", "update")
        if not data_type:
            return json_response(
                {"error": "invalid_payload", "detail": "data_type is required."}, 400
            )

        callback_url = body.get("callback_url") or _default_callback_url(service)
        if not callback_url:
            return json_response(
                {
                    "error": "invalid_payload",
                    "detail": "callback_url is required when service.public_host is not configured.",
                },
                400,
            )

        return await _subscription_call(
            lambda: client.create_subscription(
                callback_url=callback_url,
                verification_token=webhooks.verification_token.get_secret_value(),
                event_type=event_type,
                data_type=data_type,
            ),
            key="subscription",
        )

    @admin_bp.route("/webhooks/subscriptions/<subscription_id>/renew", methods=["PUT"])
    @guard
    @injector.inject
    async def renew_subscription(subscription_id: str, client: WebhookSubscriptionClient):
        """Extend a subscription's expiration_time before Oura drops it."""
        return await _subscription_call(
            lambda: client.renew_subscription(subscription_id), key="subscription"
        )

    @admin_bp.route("/webhooks/subscriptions/<subscription_id>", methods=["DELETE"])
    @guard
    @injector.inject
    async def delete_subscription(subscription_id: str, client: WebhookSubscriptionClient):
        """Cancel a subscription."""
        return await _subscription_call(
            lambda: client.delete_subscription(subscription_id), key="deleted"
        )

    return admin_bp


async def _subscription_call(call, *, key: str):
    """Make one subscription call and shape the result.

    Failures report only the upstream status: the request headers carry the
    client secret, so nothing derived from them may reach the response.
    """
    try:
        result = await call()
    except httpx2.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("oura subscription call failed", extra={"upstream_status": status})
        return json_response(
            {
                "error": "upstream_error",
                "detail": f"Oura rejected the subscription request (HTTP {status}).",
            },
            502,
        )
    except httpx2.HTTPError as exc:
        logger.warning("could not reach oura", extra={"error": type(exc).__name__})
        return json_response(
            {"error": "upstream_error", "detail": "Could not reach the Oura API."}, 502
        )
    return json_response({key: result if result is not None else True})


def _default_callback_url(service: ServiceConfig) -> str | None:
    return f"https://{service.public_host}/webhooks/oura" if service.public_host else None
