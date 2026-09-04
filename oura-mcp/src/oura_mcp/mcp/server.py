"""MCP server construction and its Streamable HTTP ASGI app."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

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
