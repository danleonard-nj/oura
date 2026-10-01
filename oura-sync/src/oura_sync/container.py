"""Composition root.

`register_common` (oura-common) wires the shared Oura/Mongo/Redis singletons;
this module registers only what oura-sync adds on top of that.

`build_provider` must run inside the serving event loop: `AsyncMongoClient` and
`redis.asyncio.Redis` bind to the loop that creates them.
"""

from __future__ import annotations

from depi import ServiceCollection, ServiceProvider

from oura_common.container import register_common

from oura_sync.config import Config
from oura_sync.repositories.snapshot_repository import SnapshotRepository
from oura_sync.services.snapshot_service import SnapshotService
from oura_sync.services.sync_service import SyncService


def register_oura_sync(services: ServiceCollection, config: Config) -> None:
    # register_common already registered `config` under CommonConfig; this
    # deployment's own services type-hint the concrete `Config` subclass, so
    # it needs a registration under that key too -- same instance either way.
    services.add_singleton(Config, factory=lambda _: config)

    services.add_singleton(SnapshotRepository)
    services.add_singleton(SnapshotService)
    services.add_singleton(SyncService)


def build_services(config: Config) -> ServiceCollection:
    services = ServiceCollection()
    register_common(services, config)
    register_oura_sync(services, config)
    return services


def build_provider(config: Config) -> ServiceProvider:
    """Build the provider, constructing singletons eagerly.

    Eager construction surfaces a wiring mistake at startup rather than on the
    first request that happens to need the broken dependency.
    """
    return build_services(config).build_provider(eager_all=True)
