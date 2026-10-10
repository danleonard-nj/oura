"""The sync and back-sync triggers and the stored-history API, behind Azure AD gateway auth."""

from __future__ import annotations

from datetime import datetime

from depi_quart import QuartInjector
from quart import Blueprint, Response, request

from app.models.config import GatewayAuthConfig, OuraConfig
from app.models.errors import OuraError
from app.models.resources import Resource
from app.repositories.snapshot_repository import SnapshotRepository
from app.services.backfill_service import BackfillService
from app.services.history_service import DEFAULT_RECORD_LIMIT, MAX_LIMIT, HistoryService
from app.services.sync_service import SyncService
from app.utilities.auth import require_gateway_token
from app.utilities.http import int_arg, json_error, json_response

def create_sync_blueprint(injector: QuartInjector) -> Blueprint:
    sync_bp = Blueprint("sync", __name__)
    guard = require_gateway_token(injector.provider.resolve(GatewayAuthConfig))

    @sync_bp.route("/internal/sync/run", methods=["POST"])
    @guard
    @injector.inject
    async def run_sync(sync: SyncService):
        """Poll the configured (or requested) resources and store any that changed.

        Called by the external cron scheduler, on whatever cadence it decides;
        the in-process `JobScheduler` covers it until then.
        """
        body, resources, problem = await _read_body()
        if problem is not None:
            return problem

        results = await sync.sync_all(resources)
        return json_response({"results": results})

    @sync_bp.route("/internal/sync/backfill", methods=["GET"])
    @guard
    @injector.inject
    async def backfill_status(backfill: BackfillService):
        """How far back each resource has been walked, and which are done."""
        return json_response({"running": backfill.running, "resources": await backfill.status()})

    @sync_bp.route("/internal/sync/backfill", methods=["POST"])
    @guard
    @injector.inject
    async def start_backfill(backfill: BackfillService):
        """Start a back-sync in the background; `{"restart": true}` walks again from today.

        Optional body: `{"resources": [...], "restart": bool}`. Answers at once
        with 202 -- a full walk takes minutes -- and GET reports progress.
        """
        body, resources, problem = await _read_body()
        if problem is not None:
            return problem

        if resources is not None:
            if unarchived := [r.value for r in resources if r not in backfill.resources]:
                return json_error(
                    f"No history to back-sync for: {', '.join(unarchived)}. Current-state and "
                    "unsynced resources are not walked.",
                    400,
                )
        if body.get("restart"):
            await backfill.reset(resources or backfill.resources)

        started = backfill.start(resources)
        return json_response(
            {"started": started, "resources": await backfill.status()}, 202 if started else 409
        )

    @sync_bp.route("/api/history/<resource>", methods=["GET"])
    @guard
    @injector.inject
    async def history(resource: str, service: HistoryService):
        """Stored history of any synced resource, oldest first -- what oura_resource_history reads."""
        try:
            selected = Resource(resource)
        except ValueError:
            return json_error(f"Unknown resource {resource!r}.", 400)

        try:
            documents = await service.get_history(
                selected,
                since=request.args.get("since"),
                until=request.args.get("until"),
                limit=int_arg("limit", default=DEFAULT_RECORD_LIMIT, maximum=MAX_LIMIT),
            )
        except OuraError as exc:
            return json_error(exc.message, exc.status_code or 400)
        return json_response({"count": len(documents), "records": documents})

    @sync_bp.route("/api/snapshots/<resource>", methods=["GET"])
    @guard
    @injector.inject
    async def snapshots(resource: str, repository: SnapshotRepository, oura: OuraConfig):
        """Historical snapshots of one resource, oldest first."""
        try:
            selected = Resource(resource)
        except ValueError:
            return json_error(f"Unknown resource {resource!r}.", 400)

        account_id = request.args.get("account_id") or oura.account_id
        documents = await repository.recent(
            selected.value,
            account_id=account_id,
            since=_parse_datetime(request.args.get("since")),
            until=_parse_datetime(request.args.get("until")),
            limit=int_arg("limit", default=100, maximum=1000),
        )
        return json_response({"count": len(documents), "snapshots": documents})

    return sync_bp


async def _read_body() -> tuple[dict, list[Resource] | None, Response | None]:
    """The optional JSON body and its `resources`, or the error response to send."""
    body: dict = {}
    if await request.get_data():
        parsed = await request.get_json(force=True, silent=True)
        if not isinstance(parsed, dict):
            return {}, None, json_error("Request body was not a JSON object.", 400)
        body = parsed

    resources = None
    if raw_resources := body.get("resources"):
        try:
            resources = [Resource(value) for value in raw_resources]
        except ValueError as exc:
            return {}, None, json_error(f"Unknown resource in request: {exc}", 400)
    return body, resources, None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
