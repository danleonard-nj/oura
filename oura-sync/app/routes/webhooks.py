"""Oura webhook endpoint.

GET  -- subscription verification: echo the challenge if the token matches.
POST -- a signed event notification.

The POST path does only what it must before acknowledging: verify, deduplicate,
bump the affected cache generations, record the event. Oura expects a response
inside 10 seconds and that budget is not close to being at risk, so there is no
background worker here.
"""

from __future__ import annotations

from pydantic import ValidationError
from depi_quart import QuartInjector
from quart import Blueprint, request

from app.models.webhooks import WebhookEvent
from app.services.webhook_service import WebhookService, WebhookVerificationError
from app.utilities.http import json_response
from app.utilities.logging import get_logger

logger = get_logger(__name__)


def create_webhooks_blueprint(injector: QuartInjector) -> Blueprint:
    webhooks_bp = Blueprint("webhooks", __name__)

    @webhooks_bp.route("/webhooks/oura", methods=["GET"])
    @injector.inject
    async def verify(service: WebhookService):
        """Answer Oura's one-time subscription challenge."""
        try:
            challenge = service.verify_challenge(
                request.args.get("verification_token"),
                request.args.get("challenge"),
            )
        except WebhookVerificationError as exc:
            logger.warning("webhook verification refused", extra={"reason": str(exc)})
            await service.record_rejection(str(exc))
            return json_response({"error": "invalid_verification_token"}, 401)

        logger.info("webhook subscription verified")
        return json_response({"challenge": challenge})

    @webhooks_bp.route("/webhooks/oura", methods=["POST"])
    @injector.inject
    async def receive(service: WebhookService):
        """Accept a signed event notification."""

        # The signature covers the bytes exactly as they arrived, so the raw body
        # must be read before any parsing.
        body = await request.get_data()

        try:
            service.verify_signature(
                request.headers.get("x-oura-signature"),
                request.headers.get("x-oura-timestamp"),
                body,
            )
        except WebhookVerificationError as exc:
            logger.warning("webhook signature rejected", extra={"reason": str(exc)})
            await service.record_rejection(str(exc))
            return json_response({"error": "invalid_signature"}, 401)

        try:
            event = WebhookEvent.model_validate_json(body)
        except ValidationError as exc:
            logger.warning("webhook payload was not a recognised event", extra={"errors": exc.error_count()})
            return json_response({"error": "invalid_payload"}, 400)

        result = await service.handle(event)
        return json_response(result, 200)

    return webhooks_bp
