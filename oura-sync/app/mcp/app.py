"""The MCP extension of the app: an `MCPServer` served by framework's MCPBlueprint.

`create_mcp_blueprint` builds the server, registers the tools, and picks the
endpoint's auth:

* OAuth (`mcp.oauth.owner_password` set) -- `with_oauth` hosts the authorization
  server in this app (`/authorize`, `/token`, `/register`, `/revoke` and the
  RFC 8414/9728 discovery documents), backed by `McpAuthService`. ChatGPT and
  other discovery-only clients need this. The static `mcp.api_key` is still
  accepted as a bearer credential for clients that can send a header.
* Otherwise -- `with_bearer` accepting `mcp.api_key` only.

The blueprint runs the SDK's Streamable HTTP session manager for as long as
Quart is serving, so `create_app` has nothing MCP-specific to do beyond
registering it.
"""

from __future__ import annotations

from depi import ServiceProvider
from framework.mcp import MCPBlueprint, shared_request_state
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp.tools import register_tools
from app.models.config import McpConfig, ServiceConfig
from app.services.mcp_auth_service import MCP_SCOPE, McpAuthService

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

REALM = "oura"


def create_mcp_server(
    mcp: McpConfig, service: ServiceConfig, provider: ServiceProvider
) -> MCPServer:
    request_state_security = None
    if mcp.request_state_secrets:
        request_state_security = shared_request_state(
            [secret.get_secret_value() for secret in mcp.request_state_secrets],
            audience=mcp.server_name,
        )

    server = MCPServer(
        name=mcp.server_name,
        title="Oura Ring",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        log_level=service.log_level,  # type: ignore[arg-type]
        request_state_security=request_state_security,
    )
    register_tools(server, provider)
    return server


def create_mcp_blueprint(
    mcp: McpConfig, service: ServiceConfig, provider: ServiceProvider
) -> MCPBlueprint:
    """Build the blueprint serving MCP on `mcp.path`.

    `transport_security` -- the SDK's DNS-rebinding guard defaults to a
    localhost-only allowlist. Production traffic arrives with the ingress
    hostname in the Host header, so `service.public_host` has to be in the allowlist or
    every request is rejected.

    `stateless_http` -- with it on, no session state lives in a replica's
    memory, so any replica can serve any request and no sticky sessions are
    needed. The trade-off is no server-initiated SSE streams between calls,
    which the tools here do not use.
    """
    blueprint = MCPBlueprint(
        "mcp",
        __name__,
        server=create_mcp_server(mcp, service, provider),
        stateless_http=mcp.stateless,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=mcp.allowed_hosts(service),
            allowed_origins=mcp.allowed_origins(service),
        ),
    )

    # McpConfig refuses an empty key, so MCP is never exposed unauthenticated.
    api_key = mcp.api_key.get_secret_value()

    if mcp.oauth.enabled:
        # Dynamic client registration is on (with_oauth's default) because a
        # client like ChatGPT cannot be pre-registered. Registration on its own
        # grants nothing -- every authorization still has to be approved on the
        # consent screen with the owner secret.
        blueprint.with_oauth(
            mcp.path,
            provider=_LazyAuthProvider(provider),  # type: ignore[arg-type]
            issuer_url=service.public_base_url,
            required_scopes=[MCP_SCOPE],
            resource_name="Oura Ring",
            api_key=api_key,
            realm=REALM,
        )
    else:
        blueprint.with_bearer(mcp.path, api_key=api_key, realm=REALM)

    return blueprint


class _LazyAuthProvider:
    """Forwards the SDK's authorization-server calls to the DI-resolved service.

    `with_oauth` needs a provider instance when the routes are built, but the
    DI provider is not built until `before_serving` runs inside the serving
    loop. This resolves on each call instead of capturing an instance that
    does not exist yet.
    """

    def __init__(self, provider: ServiceProvider) -> None:
        self._provider = provider

    def _service(self) -> McpAuthService:
        return self._provider.resolve(McpAuthService)

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
