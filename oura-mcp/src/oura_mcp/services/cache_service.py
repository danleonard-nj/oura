"""Short-lived Redis cache for Oura responses.

Invalidation uses a per-resource generation counter rather than key scanning:
a webhook bumps the counter, every key minted under the old generation becomes
unreachable, and Redis expires them on its own. That is one INCR instead of a
SCAN over an unbounded keyspace.

The cache is strictly optional. If Redis is unavailable, reads report
`unavailable` and the caller goes to Oura. Only the OAuth refresh lock treats
Redis as mandatory, because that one is a correctness requirement.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import get_logger
from oura_mcp.models.audit import CacheStatus
from oura_mcp.models.dates import DateRange, is_recent
from oura_mcp.models.resources import Resource, spec_for

logger = get_logger(__name__)

KEY_PREFIX = "oura:v1"


class CacheService:
    def __init__(self, redis: Redis, config: Config) -> None:
        self._redis = redis
        self._enabled = config.cache_enabled
        self._ttl_recent = config.cache_ttl_recent_seconds
        self._ttl_historical = config.cache_ttl_historical_seconds
        self._ttl_static = config.cache_ttl_static_seconds

    # ── Reads and writes ──────────────────────────────────────────────────

    async def get(
        self, account_id: str, resource: Resource, params: dict[str, Any]
    ) -> tuple[Any | None, CacheStatus]:
        if not self._enabled:
            return None, CacheStatus.DISABLED
        try:
            key = await self._key(account_id, resource, params)
            raw = await self._redis.get(key)
        except RedisError as exc:
            logger.warning(
                "cache read failed", extra={"resource": resource.value, "error": str(exc)}
            )
            return None, CacheStatus.UNAVAILABLE

        if raw is None:
            return None, CacheStatus.MISS
        try:
            return json.loads(raw), CacheStatus.HIT
        except json.JSONDecodeError:
            # A corrupt entry is not worth failing a request over.
            return None, CacheStatus.MISS

    async def set(
        self,
        account_id: str,
        resource: Resource,
        params: dict[str, Any],
        value: Any,
        *,
        date_range: DateRange | None = None,
    ) -> None:
        if not self._enabled:
            return
        ttl = self.ttl_for(resource, date_range)
        if ttl <= 0:
            return
        try:
            key = await self._key(account_id, resource, params)
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl)
        except RedisError as exc:
            logger.warning(
                "cache write failed", extra={"resource": resource.value, "error": str(exc)}
            )

    def ttl_for(self, resource: Resource, date_range: DateRange | None) -> int:
        """Pick a TTL from how volatile the data is.

        Static profile data holds for a day. A range that reaches into the last
        couple of days is still being amended as the ring syncs, so it gets the
        short TTL; anything older is settled.
        """
        if spec_for(resource).static:
            return self._ttl_static
        if date_range is None or is_recent(date_range):
            return self._ttl_recent
        return self._ttl_historical

    # ── Invalidation ──────────────────────────────────────────────────────

    async def invalidate(self, account_id: str, resource: Resource) -> bool:
        """Retire every cached entry for one resource. Returns False if Redis failed."""
        try:
            await self._redis.incr(self._generation_key(account_id, resource))
            return True
        except RedisError as exc:
            logger.warning(
                "cache invalidation failed",
                extra={"resource": resource.value, "error": str(exc)},
            )
            return False

    # ── Key construction ──────────────────────────────────────────────────

    def _generation_key(self, account_id: str, resource: Resource) -> str:
        return f"{KEY_PREFIX}:gen:{account_id}:{resource.value}"

    async def _key(self, account_id: str, resource: Resource, params: dict[str, Any]) -> str:
        generation = await self._redis.get(self._generation_key(account_id, resource)) or "0"
        return f"{KEY_PREFIX}:{account_id}:{resource.value}:g{generation}:{fingerprint(params)}"


def fingerprint(params: dict[str, Any]) -> str:
    """Order-independent hash of the normalised request parameters."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]
