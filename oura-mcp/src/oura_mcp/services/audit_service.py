"""Audit writing that never takes down the operation it is describing."""

from __future__ import annotations

from oura_mcp.infrastructure.logging import get_logger
from oura_mcp.models.audit import AuditEvent
from oura_mcp.repositories.audit_repository import AuditRepository

logger = get_logger(__name__)


class AuditService:
    """Records audit events to Mongo and mirrors them to structured stdout.

    Kubernetes log collection must keep working when Mongo does not, so every
    event is logged either way and a Mongo failure is swallowed with a warning.
    A successful MCP request that failed only to write its own audit row is
    still a successful request.
    """

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    async def record(self, event: AuditEvent) -> None:
        logger.info(
            f"{event.action.value} {event.status}",
            extra={
                "correlation_id": event.correlation_id,
                "action": event.action.value,
                "account_id": event.account_id,
                "tool": event.tool,
                "resource": event.resource,
                "date_range": _range_text(event),
                "duration_ms": event.duration_ms,
                "cache": event.cache.value if event.cache else None,
                "record_count": event.record_count,
                "upstream_status": event.upstream_status,
                "result_status": event.status,
                "error_category": event.error_category,
            },
        )
        try:
            await self._repository.record(event)
        except Exception as exc:  # noqa: BLE001 - audit must never fail the request
            logger.warning(
                "audit persistence failed",
                extra={
                    "correlation_id": event.correlation_id,
                    "action": event.action.value,
                    "error": str(exc),
                },
            )


def _range_text(event: AuditEvent) -> str | None:
    if event.start_date is None and event.end_date is None:
        return None
    return f"{event.start_date}..{event.end_date}"
