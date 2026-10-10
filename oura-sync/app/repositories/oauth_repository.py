"""Storage for the MCP OAuth authorization server.

Split by lifetime, not by tidiness:

* **Mongo** holds registered clients and refresh tokens. Both must survive a
  Redis flush and a replica restart -- a client that has to re-register, or a
  connector that silently logs out, is a bad experience.
* **Redis** holds pending authorizations, authorization codes and access
  tokens. All three are short-lived, high-read, and want a TTL rather than a
  sweeper. Authorization codes additionally need single-use semantics, which
  `GETDEL` gives atomically across replicas.

Access tokens are stored hashed. A leaked database dump should not be a set of
working bearer tokens.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

OAUTH_CLIENTS = "oauth_clients"
OAUTH_REFRESH_TOKENS = "oauth_refresh_tokens"

PENDING_PREFIX = "mcp:oauth:pending"
CODE_PREFIX = "mcp:oauth:code"
ACCESS_PREFIX = "mcp:oauth:access"
ATTEMPTS_PREFIX = "mcp:oauth:attempts"


def fingerprint(token: str) -> str:
    """Hash a token for storage and lookup."""
    return hashlib.sha256(token.encode()).hexdigest()


class OAuthRepository:
    def __init__(self, database: AsyncDatabase, redis: Redis) -> None:
        self._clients = database[OAUTH_CLIENTS]
        self._refresh = database[OAUTH_REFRESH_TOKENS]
        self._redis = redis

    # ── Registered clients (durable) ──────────────────────────────────────

    async def save_client(self, client_id: str, document: dict[str, Any]) -> None:
        await self._clients.update_one(
            {"client_id": client_id},
            {"$set": {"client_id": client_id, "document": document}},
            upsert=True,
        )

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        record = await self._clients.find_one({"client_id": client_id})
        return record["document"] if record else None

    async def list_clients(self) -> list[dict[str, Any]]:
        cursor = self._clients.find({}, projection={"_id": False})
        return [record async for record in cursor]

    async def delete_client(self, client_id: str) -> bool:
        result = await self._clients.delete_one({"client_id": client_id})
        return result.deleted_count > 0

    # ── Pending authorizations (ephemeral) ────────────────────────────────

    async def put_pending(self, txn_id: str, payload: dict[str, Any], ttl: int) -> None:
        await self._redis.set(f"{PENDING_PREFIX}:{txn_id}", json.dumps(payload), ex=ttl)

    async def peek_pending(self, txn_id: str) -> dict[str, Any] | None:
        """Read without consuming, so a failed password attempt can retry."""
        raw = await self._redis.get(f"{PENDING_PREFIX}:{txn_id}")
        return json.loads(raw) if raw else None

    async def take_pending(self, txn_id: str) -> dict[str, Any] | None:
        raw = await self._redis.getdel(f"{PENDING_PREFIX}:{txn_id}")
        return json.loads(raw) if raw else None

    # ── Authorization codes (ephemeral, single-use) ───────────────────────

    async def put_code(self, code: str, payload: dict[str, Any], ttl: int) -> None:
        await self._redis.set(f"{CODE_PREFIX}:{fingerprint(code)}", json.dumps(payload), ex=ttl)

    async def peek_code(self, code: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{CODE_PREFIX}:{fingerprint(code)}")
        return json.loads(raw) if raw else None

    async def take_code(self, code: str) -> dict[str, Any] | None:
        """Consume atomically. A replayed code finds nothing, on any replica."""
        raw = await self._redis.getdel(f"{CODE_PREFIX}:{fingerprint(code)}")
        return json.loads(raw) if raw else None

    # ── Access tokens (ephemeral) ─────────────────────────────────────────

    async def put_access_token(self, token: str, payload: dict[str, Any], ttl: int) -> None:
        await self._redis.set(f"{ACCESS_PREFIX}:{fingerprint(token)}", json.dumps(payload), ex=ttl)

    async def get_access_token(self, token: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{ACCESS_PREFIX}:{fingerprint(token)}")
        return json.loads(raw) if raw else None

    async def delete_access_token(self, token: str) -> None:
        await self._redis.delete(f"{ACCESS_PREFIX}:{fingerprint(token)}")

    # ── Refresh tokens (durable) ──────────────────────────────────────────

    async def put_refresh_token(self, token: str, payload: dict[str, Any]) -> None:
        await self._refresh.update_one(
            {"token_hash": fingerprint(token)},
            {"$set": {"token_hash": fingerprint(token), **payload}},
            upsert=True,
        )

    async def get_refresh_token(self, token: str) -> dict[str, Any] | None:
        return await self._refresh.find_one(
            {"token_hash": fingerprint(token)}, projection={"_id": False}
        )

    async def delete_refresh_token(self, token: str) -> bool:
        result = await self._refresh.delete_one({"token_hash": fingerprint(token)})
        return result.deleted_count > 0

    # ── Consent brute-force guard ─────────────────────────────────────────

    async def record_failed_attempt(self, key: str, ttl: int) -> int:
        """Count a failed consent attempt within a rolling window."""
        redis_key = f"{ATTEMPTS_PREFIX}:{key}"
        attempts = await self._redis.incr(redis_key)
        if attempts == 1:
            await self._redis.expire(redis_key, ttl)
        return int(attempts)

    async def failed_attempts(self, key: str) -> int:
        value = await self._redis.get(f"{ATTEMPTS_PREFIX}:{key}")
        return int(value) if value else 0

    async def clear_failed_attempts(self, key: str) -> None:
        await self._redis.delete(f"{ATTEMPTS_PREFIX}:{key}")
