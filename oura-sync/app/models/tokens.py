"""OAuth token records.

`OuraToken` is the in-memory, plaintext shape. What lands in Mongo is the
encrypted document produced by TokenRepository; the two are deliberately
separate so plaintext is never accidentally serialised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field


class OuraToken(BaseModel):
    """A live OAuth token pair. Never returned through the API or MCP."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_at: datetime
    scopes: list[str] = Field(default_factory=list)
    token_type: str = "Bearer"
    #: Monotonically increasing; bumped on every persisted refresh. Lets a
    #: replica see that another one rotated the token while it waited for the lock.
    version: int = 1
    updated_at: datetime | None = None

    def is_expired(self, *, skew_seconds: int = 0) -> bool:
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=skew_seconds)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"OuraToken(account_id={self.account_id!r}, expires_at={self.expires_at!r}, "
            f"version={self.version}, scopes={self.scopes!r})"
        )

    __str__ = __repr__


class TokenGrant(BaseModel):
    """A raw token response from Oura's /oauth/token endpoint."""

    model_config = ConfigDict(frozen=True)

    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_in: int
    token_type: str = "Bearer"
    scope: str = ""

    def to_token(self, account_id: str, *, version: int = 1) -> OuraToken:
        return OuraToken(
            account_id=account_id,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.expires_in),
            scopes=self.scope.split() if self.scope else [],
            token_type=self.token_type,
            version=version,
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"TokenGrant(expires_in={self.expires_in}, scope={self.scope!r})"

    __str__ = __repr__


class ConnectionStatus(BaseModel):
    """Safe-to-expose summary of whether an account is connected."""

    account_id: str
    connected: bool
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None
    version: int | None = None
