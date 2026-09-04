"""MCP server construction and its Streamable HTTP ASGI app."""

from __future__ import annotations

from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Route

from oura_mcp.config import Config
from oura_mcp.mcp.context import ToolContext
from oura_mcp.mcp.tools import register_tools

INSTRUCTIONS = """\
Read-only access to the user's Oura Ring data through the Oura API v2.

Start with `oura_summary` for anything spanning several metrics or several days;
it joins sleep, readiness, activity, stress, resilience, SpO2 and workouts on the
calendar day in one call. Reach for a single-metric tool when the question is
narrow, and `oura_query` only for collections without a dedicated tool.

Dates are ISO-8601 (YYYY-MM-DD) and ranges are inclusive. Omitting both dates
gives a sensible recent window per tool. Oura keys daily records by the `day`
they belong to, which for sleep is the morning the night ended.

This data is personal health information. Do not repeat it beyond what answering
the question requires.\
"""


def build_mcp_server(config: Config, context: ToolContext) -> MCPServer:
    server = MCPServer(
        name=config.mcp_server_name,
        title="Oura Ring",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        log_level=config.log_level,  # type: ignore[arg-type]
    )
    register_tools(server, context)
    return server


def build_mcp_app(server: MCPServer, config: Config) -> Starlette:
    """Build the Streamable HTTP ASGI app.

    Two deployment details matter here:

    `transport_security` -- the SDK's DNS-rebinding guard defaults to a
    localhost-only allowlist. Production traffic arrives with the ingress
    hostname in the Host header, so PUBLIC_HOST has to be in the allowlist or
    every request is rejected.

    `stateless_http` -- with it on, no session state lives in a replica's
    memory, so any replica can serve any request and no sticky sessions are
    needed. The trade-off is no server-initiated SSE streams between calls,
    which the tools here do not use.

    The returned app carries a lifespan that starts the session manager, but a
    mounted sub-app never has its lifespan run by the parent. `create_app`
    therefore runs `server.session_manager.run()` from the root lifespan.
    """
    return server.streamable_http_app(
        streamable_http_path=config.mcp_path,
        stateless_http=config.mcp_stateless,
        host=config.public_host or config.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.allowed_hosts,
            allowed_origins=config.allowed_origins,
        ),
    )


class _LazyAuthProvider:
    """Forwards the SDK's authorization-server calls to the DI-resolved service.

    The SDK's route factories need a provider instance when the routes are
    built, but the DI provider is not constructed until the lifespan runs
    inside the serving loop. This resolves on each call instead of capturing
    an instance that does not exist yet.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def _service(self):
        from oura_mcp.services.mcp_auth_service import McpAuthService

        return self._context.resolve(McpAuthService)

    async def get_client(self, client_id):
        return await self._service().get_client(client_id)

    async def register_client(self, client_info):
        return await self._service().register_client(client_info)

    async def authorize(self, client, params):
        return await self._service().authorize(client, params)

    async def load_authorization_code(self, client, authorization_code):
        return await self._service().load_authorization_code(client, authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        return await self._service().exchange_authorization_code(client, authorization_code)

    async def load_refresh_token(self, client, refresh_token):
        return await self._service().load_refresh_token(client, refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await self._service().exchange_refresh_token(client, refresh_token, scopes)

    async def load_access_token(self, token):
        return await self._service().load_access_token(token)

    async def revoke_token(self, token):
        return await self._service().revoke_token(token)


def build_oauth_routes(
    config: Config, context: ToolContext
) -> tuple[list[Route], str | None]:
    """Build the OAuth authorization-server and metadata routes.

    Returns the routes for the root application plus the protected-resource
    metadata URL, which the 401 challenge advertises so a client can discover
    where to authorize.

    These belong on the root app: the MCP transport is attached at exactly
    `/mcp`, so routes the SDK created beside it would never be reached.

    Dynamic client registration is enabled because a client like ChatGPT cannot
    be pre-registered. Registration on its own grants nothing -- every
    authorization still has to be approved on the consent screen with the owner
    secret.
    """
    if not config.mcp_oauth_enabled:
        return [], None

    from oura_mcp.services.mcp_auth_service import MCP_SCOPE

    issuer_url = AnyHttpUrl(config.public_base_url)
    resource_url = AnyHttpUrl(f"{config.public_base_url}{config.mcp_path}")
    provider = _LazyAuthProvider(context)

    routes: list[Route] = [
        *create_auth_routes(
            provider=provider,  # type: ignore[arg-type]
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_SCOPE],
                default_scopes=[MCP_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        *create_protected_resource_routes(
            resource_url=resource_url,
            authorization_servers=[issuer_url],
            scopes_supported=[MCP_SCOPE],
            resource_name="Oura Ring",
        ),
    ]
    return routes, str(build_resource_metadata_url(resource_url))
