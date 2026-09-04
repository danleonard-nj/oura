"""Composition root.

Every dependency in the application is registered here and nowhere else. Nothing
below this module constructs its own Mongo client, Redis client or service; they
receive what they need through their constructors.

Lifetimes:

* singleton for shared clients and anything expensive or connection-bound, and
  for services that hold no per-request state -- which is all of them here,
* transient for nothing yet; no service in this application benefits from it,
* scoped for nothing: there is no per-request state to scope. The correlation id
  travels in a context variable, which is cheaper and works identically inside
  MCP tool execution, where there is no HTTP request object to hang a scope on.

`build_provider` must run inside the serving event loop: `AsyncMongoClient` and
`redis.asyncio.Redis` bind to the loop that creates them.
"""

from __future__ import annotations

from depi import ServiceCollection, ServiceProvider
from httpx2 import AsyncClient, Limits, Timeout
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from oura_mcp.clients.oauth_client import OuraOAuthClient
from oura_mcp.clients.oura_client import OuraApiClient, WebhookSubscriptionClient
from oura_mcp.config import Config
from oura_mcp.infrastructure.crypto import TokenCipher
from oura_mcp.infrastructure.mongo import create_database, create_mongo_client
from oura_mcp.infrastructure.redis import create_redis_client
from oura_mcp.repositories.audit_repository import AuditRepository
from oura_mcp.repositories.oauth_repository import OAuthRepository
from oura_mcp.repositories.token_repository import TokenRepository
from oura_mcp.repositories.webhook_repository import WebhookRepository
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.cache_service import CacheService
from oura_mcp.services.mcp_auth_service import McpAuthService
from oura_mcp.services.oauth_service import OAuthService
from oura_mcp.services.oura_service import OuraService
from oura_mcp.services.summary_service import SummaryService
from oura_mcp.services.token_service import TokenService
from oura_mcp.services.webhook_service import WebhookService


def register_infrastructure(services: ServiceCollection, config: Config) -> None:
    # Registered through a factory rather than `instance=`: pydepi introspects
    # the constructor of an instance registration, and pydantic-settings
    # generates an __init__ full of annotations that are not injectable types.
    services.add_singleton(Config, factory=lambda _: config)
    services.add_singleton(TokenCipher)
    services.add_singleton(
        AsyncClient, factory=lambda provider: _http_client(provider.resolve(Config))
    )
    services.add_singleton(
        AsyncMongoClient, factory=lambda provider: create_mongo_client(provider.resolve(Config))
    )
    services.add_singleton(
        AsyncDatabase,
        factory=lambda provider: create_database(
            provider.resolve(AsyncMongoClient), provider.resolve(Config)
        ),
    )
    services.add_singleton(
        Redis, factory=lambda provider: create_redis_client(provider.resolve(Config))
    )


def register_clients(services: ServiceCollection) -> None:
    services.add_singleton(OuraApiClient)
    services.add_singleton(OuraOAuthClient)
    services.add_singleton(WebhookSubscriptionClient)


def register_repositories(services: ServiceCollection) -> None:
    services.add_singleton(TokenRepository)
    services.add_singleton(AuditRepository)
    services.add_singleton(OAuthRepository)
    services.add_singleton(WebhookRepository)


def register_services(services: ServiceCollection) -> None:
    services.add_singleton(AuditService)
    services.add_singleton(CacheService)
    services.add_singleton(TokenService)
    services.add_singleton(OuraService)
    services.add_singleton(SummaryService)
    services.add_singleton(OAuthService)
    services.add_singleton(WebhookService)
    services.add_singleton(McpAuthService)


def build_services(config: Config) -> ServiceCollection:
    services = ServiceCollection()
    register_infrastructure(services, config)
    register_clients(services)
    register_repositories(services)
    register_services(services)
    return services


def build_provider(config: Config) -> ServiceProvider:
    """Build the provider, constructing singletons eagerly.

    Eager construction surfaces a wiring mistake at startup rather than on the
    first request that happens to need the broken dependency.
    """
    return build_services(config).build_provider(eager_all=True)


def _http_client(config: Config) -> AsyncClient:
    """One pooled HTTP client for every outbound call.

    Shared rather than per-client so connection reuse and the connection
    ceiling apply across the whole application. `oura_executor_workers` is now
    a cap on concurrent upstream connections rather than a thread count -- Oura
    rate-limits per token and per application, so the ceiling is still the
    useful behaviour, just without the threads.
    """
    return AsyncClient(
        timeout=Timeout(
            connect=5.0,
            read=config.oura_request_timeout_seconds,
            write=10.0,
            pool=5.0,
        ),
        limits=Limits(
            max_connections=config.oura_executor_workers,
            max_keepalive_connections=max(2, config.oura_executor_workers // 2),
            keepalive_expiry=30.0,
        ),
        follow_redirects=False,
        headers={"User-Agent": "oura-mcp/0.1.0"},
    )
