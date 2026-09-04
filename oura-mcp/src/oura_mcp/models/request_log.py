"""HTTP-level request log entry written to Mongo for every request served.

Deliberately separate from `audit_events`: that collection is a domain audit
trail keyed by account and MCP action (`tool_invoked`, `oura_request`, ...),
and would be swamped by the mass of opportunistic bot traffic (scanners
probing for `/.env`, `/wp-admin/setup-config.php`, and the like) that this
collection exists to make visible. This is a short-retention, high-volume
sink -- see the TTL index in `infrastructure.mongo.ensure_indexes` -- not a
permanent record.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class RequestLogEntry(BaseModel):
    """One row in `request_log`."""

    correlation_id: str
    method: str
    path: str
    status: int | None = None
    duration_ms: float
    bytes: int
    client_ip: str | None = None
    user_agent: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
