"""Liveness and readiness probes."""

from __future__ import annotations

from depi_quart import QuartInjector
from pymongo import AsyncMongoClient
from quart import Blueprint
from redis.asyncio import Redis

from app.utilities import mongo, redis as redis_infra
from app.utilities.http import json_response


def create_health_blueprint(injector: QuartInjector) -> Blueprint:
    health_bp = Blueprint("health", __name__)

    @health_bp.route("/health", methods=["GET"])
    async def health():
        """Liveness. Deliberately dependency-free.

        Kubernetes restarts the pod when this fails, so it must not go red because
        Mongo, Redis or Oura is having a bad day -- restarting would not help.
        """
        return json_response({"status": "ok"})

    @health_bp.route("/ready", methods=["GET"])
    @injector.inject
    async def ready(mongo_client: AsyncMongoClient, redis: Redis):
        """Readiness. Checks the dependencies needed to serve a request.

        Mongo holds the OAuth token and the snapshots, and Redis guards token
        refresh, so without either the service cannot safely answer an MCP call
        or a sync run. Oura itself is never called here: a probe every few seconds
        would burn rate limit for no benefit.
        """
        checks = {
            "mongo": await mongo.ping(mongo_client),
            "redis": await redis_infra.ping(redis),
        }
        ready_now = all(checks.values())
        return json_response(
            {"status": "ready" if ready_now else "not_ready", "checks": checks},
            200 if ready_now else 503,
        )

    return health_bp
