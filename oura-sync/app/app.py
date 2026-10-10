"""The application: one Quart app serving everything.

    /                     landing page
    /health, /ready       probes
    /internal/sync/run    snapshot sync, triggered by the external cron scheduler
    /internal/sync/backfill
                          back-sync status and trigger (Azure AD gateway auth)
    /api/snapshots/<r>    stored snapshots (Azure AD gateway auth)
    /api/history/<r>      stored history of any resource (Azure AD gateway auth)
    /api/wear/*, /internal/wear/*
                          ring wear status, history, poll and backfill (Azure AD gateway auth)
    /api/*                administrative status, audit, request log, webhooks
    /oauth/*              Oura authorization code flow
    /webhooks/oura        Oura webhook verification and events
    /mcp                  MCP Streamable HTTP, behind bearer/OAuth auth
    /consent, /authorize, /token, /register, /revoke, /.well-known/*
                          MCP OAuth authorization server (when enabled)

The MCP endpoint is an extension of this app rather than a separate one:
framework's `MCPBlueprint` (see `app.mcp.app`) serves the SDK's transport from
a Quart route, so MCP tools and HTTP routes share one DI provider, one Oura
client, one token refresh lock and one response cache.

Views get their dependencies from depi's `QuartInjector` (autowired by type).
Configuration is framework's `Configuration`: `config.json` in the working
directory (`config.dev.json` / `config.local.json` by `FLASK_ENV`/`QUART_ENV`),
dropped in by CI/CD and never committed.

Serve it with `hypercorn "app.app:create_app()"`. Building on demand rather
than at import time keeps the module importable without a config file, which
is what the test suite relies on.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from depi import ServiceProvider
from depi_quart import QuartInjector
from framework.configuration.configuration import Configuration
from httpx2 import AsyncClient
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from quart import Quart
from redis.asyncio import Redis

from app.mcp.app import create_mcp_blueprint
from app.models.config import (
    CacheConfig,
    GatewayAuthConfig,
    McpConfig,
    OuraConfig,
    ServiceConfig,
    SyncConfig,
    WearConfig,
)
from app.routes import (
    create_admin_blueprint,
    create_consent_blueprint,
    create_health_blueprint,
    create_oauth_blueprint,
    create_sync_blueprint,
    create_wear_blueprint,
    create_webhooks_blueprint,
    home_bp,
)
from app.utilities.logging import configure_logging, get_logger
from app.utilities.middleware import CorrelationIdMiddleware
from app.services.backfill_service import BackfillService
from app.services.scheduler import JobScheduler
from app.utilities.mongo import ensure_indexes
from app.utilities.provider import create_provider

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> Quart:
    provider = create_provider()

    # Config sections are plain values, safe to resolve before the serving
    # loop exists -- and resolving them here fails fast on a bad config.json.
    service = provider.resolve(ServiceConfig)
    mcp = provider.resolve(McpConfig)
    configure_logging(service.log_level)

    app = Quart(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

    injector = QuartInjector(provider, autowire=True)
    injector.setup(app)

    for blueprint in (
        create_health_blueprint(injector),
        home_bp,
        create_admin_blueprint(injector),
        create_oauth_blueprint(injector),
        create_webhooks_blueprint(injector),
        create_sync_blueprint(injector),
        create_wear_blueprint(injector),
        create_consent_blueprint(injector),
    ):
        app.register_blueprint(blueprint)
    app.register_blueprint(create_mcp_blueprint(mcp, service, provider))

    @app.before_serving
    async def startup() -> None:
        # Built here, not at assembly, so loop-bound clients (Mongo, Redis)
        # attach to the loop that will actually serve requests.
        await provider.build_async()

        try:
            await ensure_indexes(provider.resolve(AsyncDatabase))
        except Exception as exc:  # noqa: BLE001 - startup boundary
            # Index creation is idempotent housekeeping; a Mongo blip here
            # should not stop the process from coming up and reporting unready.
            logger.warning("could not ensure mongo indexes", extra={"error": str(exc)})

        logger.info(
            "oura-sync starting",
            extra={
                "environment": provider.resolve(Configuration).environment,
                "mcp_path": mcp.path,
                "stateless": mcp.stateless,
                "public_host": service.public_host or None,
                "account_id": provider.resolve(OuraConfig).account_id,
                "cache_enabled": provider.resolve(CacheConfig).enabled,
                "mcp_oauth": mcp.oauth.enabled,
                "sync_resources": [r.value for r in provider.resolve(SyncConfig).resources],
                "gateway_auth_configured": provider.resolve(GatewayAuthConfig).configured,
                "sync_interval_hours": provider.resolve(SyncConfig).interval_hours,
                "backfill_enabled": provider.resolve(SyncConfig).backfill_enabled,
                "wear_poll_interval_minutes": provider.resolve(WearConfig).poll_interval_minutes,
            },
        )

        # Sync, back-sync and the ring wear poll; see services/scheduler.py.
        provider.resolve(JobScheduler).start()

    @app.after_serving
    async def shutdown() -> None:
        with contextlib.suppress(Exception):
            provider.resolve(JobScheduler).shutdown()
        with contextlib.suppress(Exception):
            await provider.resolve(BackfillService).aclose()
        await _shutdown(provider)

    # Outermost, so it sees every request -- MCP included -- and every
    # response, including Quart's own error pages.
    app.asgi_app = CorrelationIdMiddleware(app.asgi_app, provider)  # type: ignore[method-assign]
    return app


async def _shutdown(provider: ServiceProvider) -> None:
    """Close what we opened, in reverse order, tolerating partial failures."""
    logger.info("oura-sync shutting down")
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncClient).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(Redis).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncMongoClient).close()
