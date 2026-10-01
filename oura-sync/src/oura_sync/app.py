"""ASGI composition root.

A small Starlette application:

    /health              liveness
    /ready               readiness
    /internal/sync/run   triggered by the external cron scheduler
    /api/snapshots/{r}   called by oura-mcp for historical snapshots

No MCP transport, no MCP OAuth, no webhooks, no consent screen -- those are
oura-mcp concerns. This service only polls Oura and answers for what it found.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from httpx2 import AsyncClient
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.middleware import Middleware

from oura_common.infrastructure.logging import configure_logging, get_logger
from oura_common.infrastructure.mongo import ensure_indexes as _ensure_common_indexes

from oura_sync.api import routes
from oura_sync.api.dependencies import CorrelationIdMiddleware
from oura_sync.config import Config, load_config
from oura_sync.container import build_provider
from oura_sync.repositories.snapshot_repository import ensure_indexes as _ensure_snapshot_indexes

logger = get_logger(__name__)


async def ensure_indexes(database: AsyncDatabase) -> None:
    await _ensure_common_indexes(database)
    await _ensure_snapshot_indexes(database)


def create_app(config: Config | None = None) -> Starlette:
    """Build the root ASGI application.

    Serve it with `hypercorn "oura_sync.app:create_app()"`. Building on demand
    rather than at import time keeps the module importable without a full
    environment, which is what the test suite relies on.
    """
    if config is None:
        load_dotenv()
        config = load_config()
    configure_logging(config.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Built here, not at import time, so loop-bound clients attach to the
        # loop that will actually serve requests.
        provider = build_provider(config)
        app.state.provider = provider
        app.state.config = config

        try:
            await ensure_indexes(provider.resolve(AsyncDatabase))
        except Exception as exc:  # noqa: BLE001 - startup boundary
            logger.warning("could not ensure mongo indexes", extra={"error": str(exc)})

        logger.info(
            "oura-sync starting",
            extra={
                "account_id": config.oura_account_id,
                "sync_resources": [r.value for r in config.sync_resource_list],
                "gateway_auth_configured": bool(config.aad_tenant_id and config.aad_audience),
            },
        )

        try:
            yield
        finally:
            await _shutdown(provider)

    return Starlette(
        routes=routes.ROUTES,
        middleware=[Middleware(CorrelationIdMiddleware)],
        lifespan=lifespan,
    )


async def _shutdown(provider) -> None:
    """Close what we opened, in reverse order, tolerating partial failures."""
    logger.info("oura-sync shutting down")
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncClient).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(Redis).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncMongoClient).close()
