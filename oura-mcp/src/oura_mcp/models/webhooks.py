"""Oura webhook event shapes."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class WebhookEvent(BaseModel):
    """The notification body Oura POSTs to the callback URL.

    Oura sends pointers, not health data: enough to know what changed and to go
    fetch it if needed.
    """

    model_config = ConfigDict(extra="allow")

    event_type: str
    data_type: str
    object_id: str
    event_time: datetime | None = None
    user_id: str | None = None

    def dedupe_key(self) -> str:
        """Stable identity for an event, so a redelivery is recognised."""
        stamp = self.event_time.isoformat() if self.event_time else "-"
        user = self.user_id or "-"
        return f"{user}:{self.data_type}:{self.event_type}:{self.object_id}:{stamp}"

    def sanitized(self) -> dict[str, object]:
        """Metadata safe to log: identifiers and types only."""
        return {
            "event_type": self.event_type,
            "data_type": self.data_type,
            "object_id": self.object_id,
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "has_user_id": self.user_id is not None,
        }


class WebhookRecord(BaseModel):
    """What gets persisted for a received event."""

    dedupe_key: str
    event_type: str
    data_type: str
    object_id: str
    event_time: datetime | None = None
    account_id: str
    correlation_id: str
    invalidated: list[str] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
