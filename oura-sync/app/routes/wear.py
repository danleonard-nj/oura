"""Ring wear status, its history, and the poll and backfill triggers, behind Azure AD gateway auth."""

from __future__ import annotations

from datetime import UTC, datetime

from depi_quart import QuartInjector
from quart import Blueprint, request

from app.models.config import GatewayAuthConfig
from app.models.errors import OuraError
from app.services.backfill_service import EARLIEST
from app.services.history_service import parse_bound
from app.services.wear_service import WearService, history_window
from app.utilities.auth import require_gateway_token
from app.utilities.http import json_error, json_response


def create_wear_blueprint(injector: QuartInjector) -> Blueprint:
    wear_bp = Blueprint("wear", __name__)
    guard = require_gateway_token(injector.provider.resolve(GatewayAuthConfig))

    @wear_bp.route("/api/wear/status", methods=["GET"])
    @guard
    @injector.inject
    async def status(wear: WearService):
        """The current wear status -- UNKNOWN whenever the evidence is stale."""
        return json_response((await wear.current()).model_dump(mode="json"))

    @wear_bp.route("/api/wear/history", methods=["GET"])
    @guard
    @injector.inject
    async def history(wear: WearService):
        """Worn / not-worn intervals and live transitions; `since`/`until` inclusive."""
        try:
            since, until = history_window(request.args.get("since"), request.args.get("until"))
        except OuraError as exc:
            return json_error(exc.message, 400)
        return json_response((await wear.history(since, until)).model_dump(mode="json"))

    @wear_bp.route("/internal/wear/poll", methods=["POST"])
    @guard
    @injector.inject
    async def poll(wear: WearService):
        """Poll now, outside the schedule. Idempotent."""
        return json_response((await wear.poll()).model_dump(mode="json"))

    @wear_bp.route("/internal/wear/backfill", methods=["POST"])
    @guard
    @injector.inject
    async def backfill(wear: WearService):
        """Derive wear days from the record archive. Optional body: `{"since", "until"}`."""
        body = await request.get_json(force=True, silent=True) if await request.get_data() else {}
        if not isinstance(body, dict):
            return json_error("Request body was not a JSON object.", 400)
        try:
            since = parse_bound(body.get("since"), "since", end_of_day=False) or datetime.combine(
                EARLIEST, datetime.min.time(), UTC
            )
            until = parse_bound(body.get("until"), "until", end_of_day=True) or datetime.now(UTC)
        except OuraError as exc:
            return json_error(exc.message, 400)
        return json_response(await wear.backfill(since, until))

    return wear_bp

