"""Collections and indexes specific to this app (not shared with oura-sync).

`oauth_tokens` and `audit_events` live in oura-common (see
`oura_common.infrastructure.mongo`) since oura-sync reads and writes them
too; the two collections here -- webhook events and the request-log firehose
-- only exist in this app today.
"""

from __future__ import annotations

from pymongo.asynchronous.database import AsyncDatabase

WEBHOOK_EVENTS = "webhook_events"
REQUEST_LOG = "request_log"

# request_log is a firehose (every request, bot scans included), not a durable
# audit trail -- see models.request_log -- so it self-prunes via a TTL index
# rather than growing forever. A week is enough to spot and review traffic
# patterns without needing dashboards or alerting stood up.
REQUEST_LOG_TTL_SECONDS = 7 * 24 * 3600


async def ensure_indexes(database: AsyncDatabase) -> None:
    """Create the indexes this app's own collections rely on. Safe on every boot."""
    await database[WEBHOOK_EVENTS].create_index("dedupe_key", unique=True)
    await database[WEBHOOK_EVENTS].create_index([("received_at", -1)])
    await database[REQUEST_LOG].create_index([("timestamp", -1)])
    await database[REQUEST_LOG].create_index("timestamp", expireAfterSeconds=REQUEST_LOG_TTL_SECONDS)
