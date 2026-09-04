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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from oura_mcp.api.dependencies import resolve
from oura_mcp.infrastructure.logging import get_logger
from oura_mcp.models.webhooks import WebhookEvent
from oura_mcp.services.webhook_service import WebhookService, WebhookVerificationError

logger = get_logger(__name__)


async def verify(request: Request) -> JSONResponse:
    """Answer Oura's one-time subscription challenge."""
    service = resolve(request, WebhookService)
    try:
        challenge = service.verify_challenge(
            request.query_params.get("verification_token"),
            request.query_params.get("challenge"),
        )
    except WebhookVerificationError as exc:
        logger.warning("webhook verification refused", extra={"reason": str(exc)})
        await service.record_rejection(str(exc))
        return JSONResponse({"error": "invalid_verification_token"}, status_code=401)

    logger.info("webhook subscription verified")
    return JSONResponse({"challenge": challenge})


async def receive(request: Request) -> JSONResponse:
    """Accept a signed event notification."""
    service = resolve(request, WebhookService)

    # The signature covers the bytes exactly as they arrived, so the raw body
    # must be read before any parsing.
    body = await request.body()

    try:
        service.verify_signature(
            request.headers.get("x-oura-signature"),
            request.headers.get("x-oura-timestamp"),
            body,
        )
    except WebhookVerificationError as exc:
        logger.warning("webhook signature rejected", extra={"reason": str(exc)})
        await service.record_rejection(str(exc))
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    try:
        event = WebhookEvent.model_validate_json(body)
    except ValidationError as exc:
        logger.warning("webhook payload was not a recognised event", extra={"errors": exc.error_count()})
        return JSONResponse({"error": "invalid_payload"}, status_code=400)

    result = await service.handle(event)
    return JSONResponse(result, status_code=200)


ROUTES = [
    Route("/webhooks/oura", verify, methods=["GET"]),
    Route("/webhooks/oura", receive, methods=["POST"]),
]
