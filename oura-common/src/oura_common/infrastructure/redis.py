"""Redis connection management."""

from __future__ import annotations

from redis.asyncio import Redis

from oura_common.config import CommonConfig
from oura_common.infrastructure.logging import get_logger

logger = get_logger(__name__)


def create_redis_client(config: CommonConfig) -> Redis:
    """Build the async Redis client bound to the serving event loop."""
    return Redis.from_url(
        config.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        health_check_interval=30,
    )


async def ping(client: Redis) -> bool:
    """Cheap readiness check. Returns False rather than raising."""
    try:
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001 - probe boundary, must not raise
        logger.warning("redis ping failed", extra={"error": str(exc)})
        return False
