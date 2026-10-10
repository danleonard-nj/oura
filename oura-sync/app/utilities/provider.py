"""Composition root.

Every dependency in the application is registered here and nowhere else --
the HTTP routes and the MCP tools resolve from the same provider, so a tool
and a route that both need `OuraService` share one instance (and one token
refresh lock, one response cache, one pooled HTTP client).

Lifetimes: singleton throughout -- nothing here holds per-request state.

Configuration follows kube-tools-api: framework's `Configuration` is a
singleton (it reads config.json on first resolve), and each typed section is a
factory reading its key from it.

`create_provider` returns the provider *unbuilt*. Views are wired to it when
the app is assembled -- `QuartInjector` only needs to know what is registered
-- but `build_async()` runs in `before_serving`, because building constructs
the factory registrations, and `AsyncMongoClient` / `redis.asyncio.Redis` bind
to the loop that creates them.
"""

from __future__ import annotations

from depi import ServiceCollection, ServiceProvider
from framework.configuration.configuration import Configuration
from httpx2 import AsyncClient, Limits, Timeout
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.clients.oauth_client import OuraOAuthClient
from app.clients.oura_client import OuraApiClient, WebhookSubscriptionClient
from app.models.config import (
    SECTIONS,
    MongoConfig,
    OuraConfig,
    RedisConfig,
    ServiceConfig,
    bind_section,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.oauth_repository import OAuthRepository
from app.repositories.record_repository import RecordRepository
from app.repositories.request_log_repository import RequestLogRepository
from app.repositories.snapshot_repository import SnapshotRepository
from app.repositories.sync_state_repository import SyncStateRepository
from app.repositories.token_repository import TokenRepository
from app.repositories.wear_repository import WearRepository
from app.repositories.webhook_repository import WebhookRepository
from app.services.audit_service import AuditService
from app.services.backfill_service import BackfillService
from app.services.cache_service import CacheService
from app.services.history_service import HistoryService
from app.services.mcp_auth_service import McpAuthService
from app.services.oauth_service import OAuthService
from app.services.oura_service import OuraService
from app.services.record_service import RecordService
from app.services.scheduler import JobScheduler
from app.services.snapshot_service import SnapshotService
from app.services.summary_service import SummaryService
from app.services.sync_service import SyncService
from app.services.token_service import TokenService
from app.services.wear_service import WearService
from app.services.webhook_service import WebhookService
from app.utilities.crypto import TokenCipher
from app.utilities.mongo import create_database, create_mongo_client
from app.utilities.redis import create_redis_client


def build_services() -> ServiceCollection:
    services = ServiceCollection()
    _register_configuration(services)
    _register_infrastructure(services)
    _register_clients(services)
    _register_repositories(services)
    _register_services(services)
    return services


def create_provider(services: ServiceCollection | None = None) -> ServiceProvider:
    """The provider, not yet built -- `before_serving` calls `build_async()`."""
    return ServiceProvider(services if services is not None else build_services())


def _register_configuration(services: ServiceCollection) -> None:
    services.add_singleton(Configuration)
    for section_type, key in SECTIONS.items():
        services.add_singleton(
            section_type,
            factory=lambda provider, section_type=section_type, key=key: bind_section(
                section_type, key, getattr(provider.resolve(Configuration), key, None)
            ),
        )


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
    services.add_singleton(RecordRepository)
    services.add_singleton(SyncStateRepository)
    services.add_singleton(WearRepository)


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
    services.add_singleton(RecordService)
    services.add_singleton(SyncService)
    services.add_singleton(BackfillService)
    services.add_singleton(HistoryService)
    services.add_singleton(WearService)
    services.add_singleton(JobScheduler)


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
