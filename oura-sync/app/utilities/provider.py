"""Composition root.

Every dependency in the application is registered here and nowhere else --
the HTTP routes and the MCP tools resolve from the same provider, so a tool
and a route that both need `OuraService` share one instance (and one token
refresh lock, one response cache, one pooled HTTP client).

Lifetimes: singleton throughout -- nothing here holds per-request state.

`build_provider` must run inside the serving event loop: `AsyncMongoClient`
and `redis.asyncio.Redis` bind to the loop that creates them. `create_app`
therefore builds it in `before_serving` and binds it to a `ContainerProvider`
that routes and tools were handed when the app was assembled.
"""

from __future__ import annotations

from typing import TypeVar

from depi import ServiceCollection, ServiceProvider
from framework.configuration.configuration import Configuration
from httpx2 import AsyncClient, Limits, Timeout
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from quart import current_app
from redis.asyncio import Redis

from app.clients.oauth_client import OuraOAuthClient
from app.clients.oura_client import OuraApiClient, WebhookSubscriptionClient
from app.models.config import (
    MongoConfig,
    OuraConfig,
    RedisConfig,
    ServiceConfig,
    bind_sections,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.oauth_repository import OAuthRepository
from app.repositories.request_log_repository import RequestLogRepository
from app.repositories.snapshot_repository import SnapshotRepository
from app.repositories.token_repository import TokenRepository
from app.repositories.webhook_repository import WebhookRepository
from app.services.audit_service import AuditService
from app.services.cache_service import CacheService
from app.services.history_service import HistoryService
from app.services.mcp_auth_service import McpAuthService
from app.services.oauth_service import OAuthService
from app.services.oura_service import OuraService
from app.services.snapshot_service import SnapshotService
from app.services.summary_service import SummaryService
from app.services.sync_service import SyncService
from app.services.token_service import TokenService
from app.services.webhook_service import WebhookService
from app.utilities.crypto import TokenCipher
from app.utilities.mongo import create_database, create_mongo_client
from app.utilities.redis import create_redis_client

T = TypeVar("T")

#: Key the `ContainerProvider` is stored under in `app.extensions`.
EXTENSION_KEY = "container"


class ContainerProvider:
    """Holds the service provider once startup has built it.

    Routes and MCP tools are registered when the app is assembled, before the
    serving loop exists; this is the one object they are all handed, bound to
    the real provider when `before_serving` runs.
    """

    def __init__(self) -> None:
        self._provider: ServiceProvider | None = None

    def bind(self, provider: ServiceProvider) -> None:
        self._provider = provider

    def release(self) -> ServiceProvider | None:
        """Unbind and return the provider, for shutdown."""
        provider, self._provider = self._provider, None
        return provider

    @property
    def ready(self) -> bool:
        return self._provider is not None

    def resolve(self, service_type: type[T]) -> T:
        if self._provider is None:
            raise RuntimeError(
                "The service provider has not been built yet; application startup "
                "has not completed."
            )
        return self._provider.resolve(service_type)


def resolve(service_type: type[T]) -> T:
    """Resolve a service for the current request from the app's container."""
    container: ContainerProvider = current_app.extensions[EXTENSION_KEY]
    return container.resolve(service_type)


def build_services(configuration: Configuration) -> ServiceCollection:
    services = ServiceCollection()
    _register_configuration(services, configuration)
    _register_infrastructure(services)
    _register_clients(services)
    _register_repositories(services)
    _register_services(services)
    return services


def build_provider(configuration: Configuration) -> ServiceProvider:
    """Build the provider, constructing singletons eagerly.

    Eager construction surfaces a wiring mistake at startup rather than on the
    first request that happens to need the broken dependency.
    """
    return build_services(configuration).build_provider(eager_all=True)


def _register_configuration(services: ServiceCollection, configuration: Configuration) -> None:
    """Framework `Configuration`, plus one typed singleton per config.json section.

    Registered through factories rather than `instance=`: pydepi introspects
    the constructor of an instance registration, and pydantic models have an
    __init__ full of annotations that are not injectable types.
    """
    services.add_singleton(Configuration, factory=lambda _: configuration)
    for section_type, section in bind_sections(configuration.values).items():
        services.add_singleton(section_type, factory=lambda _, section=section: section)


def _register_infrastructure(services: ServiceCollection) -> None:
    services.add_singleton(TokenCipher)
    services.add_singleton(
        AsyncClient,
        factory=lambda provider: _http_client(
            provider.resolve(OuraConfig), provider.resolve(ServiceConfig)
        ),
    )
    services.add_singleton(
        AsyncMongoClient,
        factory=lambda provider: create_mongo_client(
            provider.resolve(MongoConfig), provider.resolve(ServiceConfig)
        ),
    )
    services.add_singleton(
        AsyncDatabase,
        factory=lambda provider: create_database(
            provider.resolve(AsyncMongoClient), provider.resolve(MongoConfig)
        ),
    )
    services.add_singleton(
        Redis, factory=lambda provider: create_redis_client(provider.resolve(RedisConfig))
    )


def _register_clients(services: ServiceCollection) -> None:
    services.add_singleton(OuraApiClient)
    services.add_singleton(OuraOAuthClient)
    services.add_singleton(WebhookSubscriptionClient)


def _register_repositories(services: ServiceCollection) -> None:
    services.add_singleton(TokenRepository)
    services.add_singleton(AuditRepository)
    services.add_singleton(OAuthRepository)
    services.add_singleton(WebhookRepository)
    services.add_singleton(RequestLogRepository)
    services.add_singleton(SnapshotRepository)


def _register_services(services: ServiceCollection) -> None:
    services.add_singleton(AuditService)
    services.add_singleton(CacheService)
    services.add_singleton(TokenService)
    services.add_singleton(OuraService)
    services.add_singleton(OAuthService)
    services.add_singleton(SummaryService)
    services.add_singleton(WebhookService)
    services.add_singleton(McpAuthService)
    services.add_singleton(SnapshotService)
    services.add_singleton(SyncService)
    services.add_singleton(HistoryService)


def _http_client(oura: OuraConfig, service: ServiceConfig) -> AsyncClient:
    """One pooled HTTP client for every outbound call.

    Shared rather than per-client so connection reuse and the connection
    ceiling apply across the whole application. `oura.executor_workers` is now
    a cap on concurrent upstream connections rather than a thread count -- Oura
    rate-limits per token and per application, so the ceiling is still the
    useful behaviour, just without the threads.
    """
    return AsyncClient(
        timeout=Timeout(
            connect=5.0,
            read=oura.request_timeout_seconds,
            write=10.0,
            pool=5.0,
        ),
        limits=Limits(
            max_connections=oura.executor_workers,
            max_keepalive_connections=max(2, oura.executor_workers // 2),
            keepalive_expiry=30.0,
        ),
        follow_redirects=False,
        headers={"User-Agent": service.name},
    )
