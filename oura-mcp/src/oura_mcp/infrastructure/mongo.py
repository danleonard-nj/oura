"""MongoDB connection management using PyMongo's native async client."""

from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import get_logger

logger = get_logger(__name__)

# Collection names are contract: oura-auth and oura-sync read/write the same
# oauth_tokens documents.
OAUTH_TOKENS = "oauth_tokens"
AUDIT_EVENTS = "audit_events"
WEBHOOK_EVENTS = "webhook_events"


def create_mongo_client(config: Config) -> AsyncMongoClient:
    """Build the async Mongo client.

    Must be called from inside the serving event loop: the client binds its
    internal topology to the running loop.
    """
    return AsyncMongoClient(
        config.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        appname="oura-mcp",
        tz_aware=True,
    )


def create_database(client: AsyncMongoClient, config: Config) -> AsyncDatabase:
    return client[config.mongodb_database]


async def ping(client: AsyncMongoClient) -> bool:
    """Cheap readiness check. Returns False rather than raising."""
    try:
        await client.admin.command("ping")
        return True
    except Exception as exc:  # noqa: BLE001 - probe boundary, must not raise
        logger.warning("mongo ping failed", extra={"error": str(exc)})
        return False


async def ensure_indexes(database: AsyncDatabase) -> None:
    """Create the indexes the service relies on. Safe to run on every boot."""
    await database[OAUTH_TOKENS].create_index("account_id", unique=True)
    await database[AUDIT_EVENTS].create_index([("timestamp", -1)])
    await database[AUDIT_EVENTS].create_index([("account_id", 1), ("timestamp", -1)])
    await database[WEBHOOK_EVENTS].create_index("dedupe_key", unique=True)
    await database[WEBHOOK_EVENTS].create_index([("received_at", -1)])
