"""Shared composition helper.

`register_common` wires every singleton that talks to Oura, Mongo, Redis or
handles tokens/audit. Each service's own `container.py` calls this once, then
registers only what it adds on top -- see `oura_mcp/container.py` and
`oura_sync/container.py`.

Lifetimes: singleton for shared clients and every service here -- all of them
hold no per-request state. `register_common` must run inside the serving
event loop: `AsyncMongoClient` and `redis.asyncio.Redis` bind to the loop that
creates them.
"""

from __future__ import annotations

from depi import ServiceCollection
from httpx2 import AsyncClient, Limits, Timeout
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from oura_common.clients.oauth_client import OuraOAuthClient
from oura_common.clients.oura_client import OuraApiClient, WebhookSubscriptionClient
from oura_common.config import CommonConfig
from oura_common.infrastructure.crypto import TokenCipher
from oura_common.infrastructure.mongo import create_database, create_mongo_client
from oura_common.infrastructure.redis import create_redis_client
from oura_common.repositories.audit_repository import AuditRepository
from oura_common.repositories.token_repository import TokenRepository
from oura_common.services.audit_service import AuditService
from oura_common.services.cache_service import CacheService
from oura_common.services.oauth_service import OAuthService
from oura_common.services.oura_service import OuraService
from oura_common.services.token_service import TokenService


def register_common(services: ServiceCollection, config: CommonConfig) -> None:
    _register_infrastructure(services, config)
    _register_clients(services)
    _register_repositories(services)
    _register_services(services)


def _register_infrastructure(services: ServiceCollection, config: CommonConfig) -> None:
    # Registered through a factory rather than `instance=`: pydepi introspects
    # the constructor of an instance registration, and pydantic-settings
    # generates an __init__ full of annotations that are not injectable types.
    services.add_singleton(CommonConfig, factory=lambda _: config)
    services.add_singleton(TokenCipher)
    services.add_singleton(
        AsyncClient, factory=lambda provider: _http_client(provider.resolve(CommonConfig))
    )
    services.add_singleton(
        AsyncMongoClient,
        factory=lambda provider: create_mongo_client(provider.resolve(CommonConfig)),
    )
    services.add_singleton(
        AsyncDatabase,
        factory=lambda provider: create_database(
            provider.resolve(AsyncMongoClient), provider.resolve(CommonConfig)
        ),
    )
    services.add_singleton(
        Redis, factory=lambda provider: create_redis_client(provider.resolve(CommonConfig))
    )


def _register_clients(services: ServiceCollection) -> None:
    services.add_singleton(OuraApiClient)
    services.add_singleton(OuraOAuthClient)
    services.add_singleton(WebhookSubscriptionClient)


def _register_repositories(services: ServiceCollection) -> None:
    services.add_singleton(TokenRepository)
    services.add_singleton(AuditRepository)


def _register_services(services: ServiceCollection) -> None:
    services.add_singleton(AuditService)
    services.add_singleton(CacheService)
    services.add_singleton(TokenService)
    services.add_singleton(OuraService)
    services.add_singleton(OAuthService)


def _http_client(config: CommonConfig) -> AsyncClient:
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
        headers={"User-Agent": config.service_name},
    )
