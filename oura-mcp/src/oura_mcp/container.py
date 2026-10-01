"""Composition root.

Every dependency in the application is registered here and nowhere else.
`register_common` (oura-common) wires the shared Oura/Mongo/Redis singletons;
this module registers only what oura-mcp adds on top of that.

`build_provider` must run inside the serving event loop: `AsyncMongoClient` and
`redis.asyncio.Redis` bind to the loop that creates them.
"""

from __future__ import annotations

from depi import ServiceCollection, ServiceProvider
from httpx2 import AsyncClient
from redis.asyncio import Redis

from oura_common.azure_ad import AzureAdTokenClient
from oura_common.container import register_common

from oura_mcp.clients.sync_client import SyncServiceClient
from oura_mcp.config import Config
from oura_mcp.repositories.oauth_repository import OAuthRepository
from oura_mcp.repositories.request_log_repository import RequestLogRepository
from oura_mcp.repositories.webhook_repository import WebhookRepository
from oura_mcp.services.history_service import HistoryService
from oura_mcp.services.mcp_auth_service import McpAuthService
from oura_mcp.services.summary_service import SummaryService
from oura_mcp.services.webhook_service import WebhookService


def register_oura_mcp(services: ServiceCollection, config: Config) -> None:
    # register_common already registered `config` under CommonConfig; this
    # deployment's own services type-hint the concrete `Config` subclass, so
    # it needs a registration under that key too -- same instance either way.
    services.add_singleton(Config, factory=lambda _: config)

    services.add_singleton(OAuthRepository)
    services.add_singleton(WebhookRepository)
    services.add_singleton(RequestLogRepository)
    services.add_singleton(SummaryService)
    services.add_singleton(WebhookService)
    services.add_singleton(McpAuthService)

    # Only needed when this deployment is configured to call oura-sync; both
    # are cheap no-op-until-used singletons otherwise.
    services.add_singleton(
        AzureAdTokenClient,
        factory=lambda provider: _azure_ad_token_client(
            provider.resolve(AsyncClient), provider.resolve(Redis), provider.resolve(Config)
        ),
    )
    services.add_singleton(SyncServiceClient)
    services.add_singleton(HistoryService)


def build_services(config: Config) -> ServiceCollection:
    services = ServiceCollection()
    register_common(services, config)
    register_oura_mcp(services, config)
    return services


def build_provider(config: Config) -> ServiceProvider:
    """Build the provider, constructing singletons eagerly.

    Eager construction surfaces a wiring mistake at startup rather than on the
    first request that happens to need the broken dependency.
    """
    return build_services(config).build_provider(eager_all=True)


def _azure_ad_token_client(http: AsyncClient, redis: Redis, config: Config) -> AzureAdTokenClient:
    return AzureAdTokenClient(
        http,
        redis,
        tenant_id=config.aad_tenant_id or "",
        client_id=config.aad_client_id or "",
        client_secret=config.aad_client_secret.get_secret_value() if config.aad_client_secret else "",
    )
