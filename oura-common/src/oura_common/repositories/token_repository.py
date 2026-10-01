"""Durable, encrypted storage for OAuth tokens.

This is a real boundary rather than decoration: it owns the encryption of token
material and the atomicity of rotation, and it defines the document shape that
oura-auth, oura-mcp and oura-sync all read and write.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo.asynchronous.database import AsyncDatabase

from oura_common.infrastructure.crypto import TokenCipher
from oura_common.infrastructure.mongo import OAUTH_TOKENS
from oura_common.models.tokens import ConnectionStatus, OuraToken


class TokenRepository:
    """Reads and writes `oauth_tokens`.

    Both token halves are Fernet-encrypted before they touch the database, and
    a rotation is written as one `update_one` so a reader can never observe a
    new access token paired with the old refresh token.
    """

    def __init__(self, database: AsyncDatabase, cipher: TokenCipher) -> None:
        self._collection = database[OAUTH_TOKENS]
        self._cipher = cipher

    async def get(self, account_id: str) -> OuraToken | None:
        document = await self._collection.find_one({"account_id": account_id})
        if document is None:
            return None
        return OuraToken(
            account_id=document["account_id"],
            access_token=self._cipher.decrypt(document["access_token"]),
            refresh_token=self._cipher.decrypt(document["refresh_token"]),
            expires_at=_as_utc(document["expires_at"]),
            scopes=document.get("scopes", []),
            token_type=document.get("token_type", "Bearer"),
            version=document.get("version", 1),
            updated_at=_as_utc(document["updated_at"]) if document.get("updated_at") else None,
        )

    async def save(self, token: OuraToken) -> OuraToken:
        """Persist a token pair, bumping `version`.

        One atomic upsert covers both halves. Returns the stored token with the
        version the database assigned.
        """
        now = datetime.now(UTC)
        result = await self._collection.find_one_and_update(
            {"account_id": token.account_id},
            {
                "$set": {
                    "access_token": self._cipher.encrypt(token.access_token),
                    "refresh_token": self._cipher.encrypt(token.refresh_token),
                    "expires_at": token.expires_at,
                    "scopes": token.scopes,
                    "token_type": token.token_type,
                    "updated_at": now,
                },
                "$setOnInsert": {"account_id": token.account_id, "created_at": now},
                "$inc": {"version": 1},
            },
            upsert=True,
            return_document=True,
        )
        return token.model_copy(update={"version": result.get("version", 1), "updated_at": now})

    async def status(self, account_id: str) -> ConnectionStatus:
        """Connection metadata with no token material in it."""
        document = await self._collection.find_one(
            {"account_id": account_id},
            projection={"expires_at": 1, "scopes": 1, "updated_at": 1, "version": 1},
        )
        if document is None:
            return ConnectionStatus(account_id=account_id, connected=False)
        return ConnectionStatus(
            account_id=account_id,
            connected=True,
            expires_at=_as_utc(document["expires_at"]) if document.get("expires_at") else None,
            scopes=document.get("scopes", []),
            updated_at=_as_utc(document["updated_at"]) if document.get("updated_at") else None,
            version=document.get("version"),
        )

    async def delete(self, account_id: str) -> bool:
        result = await self._collection.delete_one({"account_id": account_id})
        return result.deleted_count > 0


def _as_utc(value: datetime) -> datetime:
    """Mongo can hand back naive datetimes when tz_aware is off somewhere."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
