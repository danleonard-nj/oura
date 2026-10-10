"""MongoDB connection management using PyMongo's native async client."""

from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.models.config import MongoConfig, ServiceConfig
from app.utilities.logging import get_logger

logger = get_logger(__name__)

# Collection names are contract: oura-auth writes the same oauth_tokens
# documents this service reads and refreshes.
OAUTH_TOKENS = "oauth_tokens"
AUDIT_EVENTS = "audit_events"
WEBHOOK_EVENTS = "webhook_events"
REQUEST_LOG = "request_log"
RESOURCE_SNAPSHOTS = "resource_snapshots"
OURA_RECORDS = "oura_records"
SYNC_STATE = "sync_state"
WEAR_STATUS = "wear_status"
WEAR_TRANSITIONS = "wear_transitions"
WEAR_DAYS = "wear_days"

# request_log is a firehose (every request, bot scans included), not a durable
# audit trail -- see models.request_log -- so it self-prunes via a TTL index
# rather than growing forever. A week is enough to spot and review traffic
# patterns without needing dashboards or alerting stood up.
REQUEST_LOG_TTL_SECONDS = 7 * 24 * 3600


def create_mongo_client(mongo: MongoConfig, service: ServiceConfig) -> AsyncMongoClient:
    """Build the async Mongo client.

    Must be called from inside the serving event loop: the client binds its
    internal topology to the running loop.
    """
    return AsyncMongoClient(
        mongo.connection_string.get_secret_value(),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        appname=service.name,
        tz_aware=True,
    )


def create_database(client: AsyncMongoClient, mongo: MongoConfig) -> AsyncDatabase:
    return client[mongo.database]


async def ping(client: AsyncMongoClient) -> bool:
    """Cheap readiness check. Returns False rather than raising."""
    try:
        await client.admin.command("ping")
        return True
    except Exception as exc:  # noqa: BLE001 - probe boundary, must not raise
        logger.warning("mongo ping failed", extra={"error": str(exc)})
        return False


async def ensure_indexes(database: AsyncDatabase) -> None:
    """Create the indexes every collection relies on. Safe to run on every boot."""
    await database[OAUTH_TOKENS].create_index("account_id", unique=True)
    await database[AUDIT_EVENTS].create_index([("timestamp", -1)])
    await database[AUDIT_EVENTS].create_index([("account_id", 1), ("timestamp", -1)])
    await database[WEBHOOK_EVENTS].create_index("dedupe_key", unique=True)
    await database[WEBHOOK_EVENTS].create_index([("received_at", -1)])
    await database[REQUEST_LOG].create_index([("timestamp", -1)])
    await database[REQUEST_LOG].create_index("timestamp", expireAfterSeconds=REQUEST_LOG_TTL_SECONDS)
    await database[RESOURCE_SNAPSHOTS].create_index(
        [("account_id", 1), ("resource", 1), ("first_seen_at", -1)]
    )
    await database[OURA_RECORDS].create_index(
        [("account_id", 1), ("resource", 1), ("record_key", 1)], unique=True
    )
    await database[OURA_RECORDS].create_index(
        [("account_id", 1), ("resource", 1), ("observed_at", -1)]
    )
    await database[SYNC_STATE].create_index([("account_id", 1), ("resource", 1)], unique=True)
    await database[WEAR_STATUS].create_index("account_id", unique=True)
    await database[WEAR_TRANSITIONS].create_index(
        [("account_id", 1), ("status", 1), ("at", 1)], unique=True
    )
    await database[WEAR_TRANSITIONS].create_index([("account_id", 1), ("at", -1)])
    await database[WEAR_DAYS].create_index([("account_id", 1), ("day", 1)], unique=True)
    await database[WEAR_DAYS].create_index([("account_id", 1), ("start", 1)])
