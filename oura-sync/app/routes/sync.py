"""The sync-trigger and snapshot-query API, behind Azure AD gateway auth."""

from __future__ import annotations

from datetime import datetime

from quart import Blueprint, request

from app.models.config import OuraConfig
from app.models.resources import Resource
from app.repositories.snapshot_repository import SnapshotRepository
from app.services.sync_service import SyncService
from app.utilities.auth import require_gateway_token
from app.utilities.http import int_arg, json_error, json_response
from app.utilities.provider import resolve

sync_bp = Blueprint("sync", __name__)


@sync_bp.route("/internal/sync/run", methods=["POST"])
@require_gateway_token
async def run_sync():
    """Poll the configured (or requested) resources and store any that changed.

    Called by the external cron scheduler, on whatever cadence it decides --
    this service has no internal scheduler of its own.
    """
    body: dict = {}
    if await request.get_data():
        parsed = await request.get_json(force=True, silent=True)
        if not isinstance(parsed, dict):
            return json_error("Request body was not a JSON object.", 400)
        body = parsed

    resources = None
    if raw_resources := body.get("resources"):
        try:
            resources = [Resource(value) for value in raw_resources]
        except ValueError as exc:
            return json_error(f"Unknown resource in request: {exc}", 400)

    results = await resolve(SyncService).sync_all(resources)
    return json_response({"results": results})


@sync_bp.route("/api/snapshots/<resource>", methods=["GET"])
@require_gateway_token
async def snapshots(resource: str):
    """Historical snapshots of one resource, oldest first."""
    try:
        selected = Resource(resource)
    except ValueError:
        return json_error(f"Unknown resource {resource!r}.", 400)

    account_id = request.args.get("account_id") or resolve(OuraConfig).account_id
    documents = await resolve(SnapshotRepository).recent(
        selected.value,
        account_id=account_id,
        since=_parse_datetime(request.args.get("since")),
        until=_parse_datetime(request.args.get("until")),
        limit=int_arg("limit", default=100, maximum=1000),
    )
    return json_response({"count": len(documents), "snapshots": documents})


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
