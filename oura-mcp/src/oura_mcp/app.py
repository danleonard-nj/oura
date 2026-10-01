"""ASGI composition root.

One Starlette application serves everything:

    /mcp            official MCP Streamable HTTP app, behind bearer auth
    /health         liveness
    /ready          readiness
    /oauth/*        authorization code flow
    /webhooks/oura  Oura webhook verification and events
    /api/*          administrative status, audit and webhook history

The MCP endpoint is attached with `Route`, not `Mount`. `Mount("/mcp", app)`
strips the prefix, leaving the sub-app to match an empty path, which Starlette
answers with a 307 redirect to `/mcp/` -- fine for a browser, wrong for an MCP
client. `Route` passes the path through unchanged, so the SDK's own
`/mcp` route matches directly.

The MCP app's own lifespan, which starts the Streamable HTTP session manager,
is never run for a nested app. The root lifespan below runs
`session_manager.run()` itself, as the SDK requires for a mounted transport.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from httpx2 import AsyncClient
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from oura_common.infrastructure.logging import configure_logging, get_logger
from oura_common.infrastructure.mongo import ensure_indexes as _ensure_common_indexes

from oura_mcp.api import consent, home, oauth, routes, webhooks
from oura_mcp.api.auth import BearerTokenMiddleware
from oura_mcp.api.dependencies import CorrelationIdMiddleware
from oura_mcp.config import Config, load_config
from oura_mcp.container import build_provider
from oura_mcp.infrastructure.mongo import ensure_indexes as _ensure_mcp_indexes
from oura_mcp.mcp.context import ToolContext
from oura_mcp.mcp.server import build_mcp_app, build_mcp_server, build_oauth_routes

logger = get_logger(__name__)


async def ensure_indexes(database: AsyncDatabase) -> None:
    """The shared oura-common indexes, plus this app's own (webhooks, request log)."""
    await _ensure_common_indexes(database)
    await _ensure_mcp_indexes(database)


def create_app(config: Config | None = None) -> Starlette:
    """Build the root ASGI application.

    Serve it with `hypercorn "oura_mcp.app:create_app()"`. Building on demand
    rather than at import time keeps the module importable without a full
    environment, which is what the test suite relies on.
    """
    if config is None:
        load_dotenv()
        config = load_config()
    configure_logging(config.log_level)

    context = ToolContext()
    mcp_server = build_mcp_server(config, context)
    mcp_app = build_mcp_app(mcp_server, config)

    # OAuth routes live on the root app, not inside the MCP sub-app. The MCP
    # app is attached at exactly `/mcp`, so anything the SDK mounted alongside
    # it -- /authorize, /token, /register, the discovery documents -- would be
    # unreachable. `build_oauth_routes` produces them for the root table.
    oauth_routes, resource_metadata_url = build_oauth_routes(config, context)

    guarded_mcp = BearerTokenMiddleware(
        mcp_app,
        config.mcp_api_key.get_secret_value(),
        token_loader=_oauth_token_loader(context) if config.mcp_oauth_enabled else None,
        resource_metadata_url=resource_metadata_url,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Built here, not at import time, so loop-bound clients attach to the
        # loop that will actually serve requests.
        provider = build_provider(config)
        app.state.provider = provider
        app.state.config = config
        context.bind(provider)

        try:
            await ensure_indexes(provider.resolve(AsyncDatabase))
        except Exception as exc:  # noqa: BLE001 - startup boundary
            # Index creation is idempotent housekeeping; a Mongo blip here
            # should not stop the process from coming up and reporting unready.
            logger.warning("could not ensure mongo indexes", extra={"error": str(exc)})

        logger.info(
            "oura-mcp starting",
            extra={
                "mcp_path": config.mcp_path,
                "stateless": config.mcp_stateless,
                "public_host": config.public_host or None,
                "account_id": config.oura_account_id,
                "cache_enabled": config.cache_enabled,
                "mcp_oauth": config.mcp_oauth_enabled,
            },
        )

        async with contextlib.AsyncExitStack() as stack:
            # The mounted MCP transport gets its session manager started here.
            await stack.enter_async_context(mcp_server.session_manager.run())
            stack.push_async_callback(_shutdown, provider)
            yield

    return Starlette(
        routes=[
            Route("/", home.index, methods=["GET"]),
            Mount("/static", app=StaticFiles(directory=home.STATIC_DIR), name="static"),
            Route(config.mcp_path, endpoint=guarded_mcp),
            *oauth_routes,
            *(consent.ROUTES if config.mcp_oauth_enabled else []),
            *routes.ROUTES,
            *oauth.ROUTES,
            *webhooks.ROUTES,
        ],
        middleware=[Middleware(CorrelationIdMiddleware)],
        lifespan=lifespan,
    )


def _oauth_token_loader(context: ToolContext):
    """Validate an OAuth access token, resolved lazily from the provider."""
    from oura_mcp.services.mcp_auth_service import McpAuthService

    async def load(token: str):
        return await context.resolve(McpAuthService).load_access_token(token)

    return load


async def _shutdown(provider) -> None:
    """Close what we opened, in reverse order, tolerating partial failures."""
    logger.info("oura-mcp shutting down")
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncClient).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(Redis).aclose()
    with contextlib.suppress(Exception):
        await provider.resolve(AsyncMongoClient).close()


