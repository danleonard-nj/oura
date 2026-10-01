"""Health probes and the sync-trigger / snapshot-query API."""

from __future__ import annotations

from datetime import datetime

from pymongo import AsyncMongoClient
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from oura_common.infrastructure import mongo, redis as redis_infra
from oura_common.infrastructure.logging import get_logger
from oura_common.models.resources import Resource

from oura_sync.api.auth import require_gateway_token
from oura_sync.api.dependencies import json_error, resolve
from oura_sync.config import Config
from oura_sync.repositories.snapshot_repository import SnapshotRepository
from oura_sync.services.sync_service import SyncService

logger = get_logger(__name__)


async def health(_: Request) -> JSONResponse:
    """Liveness. Deliberately dependency-free."""
    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> JSONResponse:
    """Readiness. Mongo holds snapshots/tokens, Redis guards token refresh."""
    provider = getattr(request.app.state, "provider", None)
    if provider is None:
        return JSONResponse(
            {"status": "not_ready", "checks": {"application": False}}, status_code=503
        )

    mongo_ok = await mongo.ping(resolve(request, AsyncMongoClient))
    redis_ok = await redis_infra.ping(resolve(request, Redis))

    checks = {"application": True, "mongo": mongo_ok, "redis": redis_ok}
    ready_now = all(checks.values())
    return JSONResponse(
        {"status": "ready" if ready_now else "not_ready", "checks": checks},
        status_code=200 if ready_now else 503,
    )


async def run_sync(request: Request) -> JSONResponse:
    """Poll the configured (or requested) resources and store any that changed.

    Called by the external cron scheduler, on whatever cadence it decides --
    this service has no internal scheduler of its own.
    """
    if (denied := require_gateway_token(request)) is not None:
        return denied

    try:
        body = await request.json() if await request.body() else {}
    except ValueError:
        return json_error("Request body was not valid JSON.", 400)

    resources = None
    if raw_resources := body.get("resources"):
        try:
            resources = [Resource(value) for value in raw_resources]
        except ValueError as exc:
            return json_error(f"Unknown resource in request: {exc}", 400)

    results = await resolve(request, SyncService).sync_all(resources)
    return JSONResponse({"results": results})


async def snapshots(request: Request) -> JSONResponse:
    """Historical snapshots of one resource, oldest first."""
    if (denied := require_gateway_token(request)) is not None:
        return denied

    try:
        resource = Resource(request.path_params["resource"])
    except ValueError:
        return json_error(
            f"Unknown resource {request.path_params['resource']!r}.", 400
        )

    account_id = request.query_params.get("account_id") or resolve(request, Config).oura_account_id
    since = _parse_datetime(request.query_params.get("since"))
    until = _parse_datetime(request.query_params.get("until"))
    limit = _int_param(request, "limit", default=100, maximum=1000)

    repository = resolve(request, SnapshotRepository)
    documents = await repository.recent(
        resource.value, account_id=account_id, since=since, until=until, limit=limit
    )
    return JSONResponse({"count": len(documents), "snapshots": documents}, status_code=200)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    Route("/internal/sync/run", run_sync, methods=["POST"]),
    Route("/api/snapshots/{resource}", snapshots, methods=["GET"]),
]
