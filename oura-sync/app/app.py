"""The application: one Quart app serving everything.

    /                     landing page
    /health, /ready       probes
    /internal/sync/run    snapshot sync, triggered by the external cron scheduler
    /api/snapshots/<r>    stored snapshots (Azure AD gateway auth)
    /api/*                administrative status, audit, request log, webhooks
    /oauth/*              Oura authorization code flow
    /webhooks/oura        Oura webhook verification and events
    /mcp                  MCP Streamable HTTP, behind bearer/OAuth auth
    /consent, /authorize, /token, /register, /revoke, /.well-known/*
                          MCP OAuth authorization server (when enabled)

The MCP endpoint is an extension of this app rather than a separate one:
framework's `MCPBlueprint` (see `app.mcp.app`) serves the SDK's transport from
a Quart route, so MCP tools and HTTP routes share one DI container, one Oura
client, one token refresh lock and one response cache.

Configuration is framework's `Configuration`: `config.json` in the working
directory (`config.dev.json` / `config.local.json` by `FLASK_ENV`/`QUART_ENV`), dropped
in by CI/CD and never committed. Every section is validated into its typed
model before the app is assembled, so a bad file stops the process here.

Serve it with `hypercorn "app.app:create_app()"`. Building on demand rather
than at import time keeps the module importable without a config file, which
is what the test suite relies on.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from depi import ServiceProvider
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
    bind_sections,
)
from app.routes import (
    admin_bp,
    consent_bp,
    health_bp,
    home_bp,
    oauth_bp,
    sync_bp,
    webhooks_bp,
)
from app.utilities.logging import configure_logging, get_logger
from app.utilities.middleware import CorrelationIdMiddleware
from app.utilities.mongo import ensure_indexes
from app.utilities.provider import EXTENSION_KEY, ContainerProvider, build_provider

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(configuration: Configuration | None = None) -> Quart:
    if configuration is None:
        configuration = Configuration()
    sections = bind_sections(configuration.values)
    service: ServiceConfig = sections[ServiceConfig]  # type: ignore[assignment]
    mcp: McpConfig = sections[McpConfig]  # type: ignore[assignment]
    configure_logging(service.log_level)

    app = Quart(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

    container = ContainerProvider()
    app.extensions[EXTENSION_KEY] = container

    for blueprint in (health_bp, home_bp, admin_bp, oauth_bp, webhooks_bp, sync_bp, consent_bp):
        app.register_blueprint(blueprint)
    app.register_blueprint(create_mcp_blueprint(mcp, service, container))

    @app.before_serving
    async def startup() -> None:
        # Built here, not at import time, so loop-bound clients attach to the
        # loop that will actually serve requests.
        provider = build_provider(configuration)
        container.bind(provider)

        try:
            await ensure_indexes(provider.resolve(AsyncDatabase))
        except Exception as exc:  # noqa: BLE001 - startup boundary
            # Index creation is idempotent housekeeping; a Mongo blip here
            # should not stop the process from coming up and reporting unready.
            logger.warning("could not ensure mongo indexes", extra={"error": str(exc)})

        logger.info(
            "oura-sync starting",
            extra={
                "environment": configuration.environment,
                "mcp_path": mcp.path,
                "stateless": mcp.stateless,
                "public_host": service.public_host or None,
                "account_id": provider.resolve(OuraConfig).account_id,
                "cache_enabled": provider.resolve(CacheConfig).enabled,
                "mcp_oauth": mcp.oauth.enabled,
                "sync_resources": [r.value for r in provider.resolve(SyncConfig).resources],
                "gateway_auth_configured": provider.resolve(GatewayAuthConfig).configured,
            },
        )

    @app.after_serving
    async def shutdown() -> None:
        provider = container.release()
        if provider is not None:
            await _shutdown(provider)

    # Outermost, so it sees every request -- MCP included -- and every
    # response, including Quart's own error pages.
    app.asgi_app = CorrelationIdMiddleware(app.asgi_app, container)  # type: ignore[method-assign]
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
