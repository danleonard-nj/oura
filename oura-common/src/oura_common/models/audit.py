"""Audit event written to Mongo for every MCP tool call and notable operation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AuditAction(StrEnum):
    TOOL_INVOKED = "tool_invoked"
    OURA_REQUEST = "oura_request"
    TOKEN_REFRESH = "token_refresh"
    TOKEN_STORED = "token_stored"
    WEBHOOK_RECEIVED = "webhook_received"
    WEBHOOK_REJECTED = "webhook_rejected"
    OAUTH_AUTHORIZE = "oauth_authorize"
    OAUTH_CALLBACK = "oauth_callback"


class CacheStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


class AuditEvent(BaseModel):
    """One row in `audit_events`.

    Deliberately carries no health data and no credentials: identifiers,
    timings and outcomes only.
    """

    action: AuditAction
    account_id: str
    correlation_id: str
    status: str = "ok"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    tool: str | None = None
    resource: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration_ms: float | None = None
    cache: CacheStatus | None = None
    record_count: int | None = None
    upstream_status: int | None = None
    error_category: str | None = None
    error_message: str | None = None
    detail: dict[str, Any] | None = None
