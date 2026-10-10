"""What every tool module shares: date parameters and the audited runner.

Every tool is thin: resolve the date range, call `OuraService`, return a typed
model. `ToolRunner.run` owns timing, auditing and the translation of
`OuraError` into `ToolError`, so no individual tool repeats it.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Awaitable, Callable, TypeVar

from depi import ServiceProvider
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.models.audit import AuditAction, AuditEvent, CacheStatus
from app.models.dates import DateRange
from app.models.errors import OuraError
from app.models.resources import Resource
from app.models.results import RecordSet, Summary
from app.services.audit_service import AuditService
from app.services.oura_service import OuraService
from app.utilities.logging import get_correlation_id, new_correlation_id, set_correlation_id

T = TypeVar("T")

StartDate = Annotated[
    str | None,
    Field(
        default=None,
        description="Inclusive start date, ISO-8601 YYYY-MM-DD. Defaults to the tool's recent window.",
    ),
]
EndDate = Annotated[
    str | None,
    Field(default=None, description="Inclusive end date, ISO-8601 YYYY-MM-DD. Defaults to today."),
]

Operation = Callable[[OuraService], Awaitable[tuple[Any, DateRange | None]]]


class ToolRunner:
    """Runs tool bodies against services from the app's provider.

    MCP tools are called by the SDK, not Quart, so the injector cannot hand
    them their services; they resolve from the provider at call time. Not at
    registration: the provider is only built once the serving loop exists.
    """

    def __init__(self, provider: ServiceProvider) -> None:
        self._provider = provider

    def resolve(self, service_type: type[T]) -> T:
        return self._provider.resolve(service_type)

    async def run(
        self,
        tool: str,
        operation: Operation,
        *,
        resource: Resource | None = None,
    ) -> tuple[Any, DateRange | None]:
        """Execute one tool body with timing, auditing and error translation.

        `operation` resolves its own date range and returns it alongside the
        result, so range validation happens inside this try block: a bad date
        reaches the model as its actual message rather than as the SDK's
        generic "Error executing tool".
        """
        if get_correlation_id() == "-":
            set_correlation_id(new_correlation_id())

        oura = self.resolve(OuraService)
        audit = self.resolve(AuditService)
        started = time.perf_counter()
        date_range: DateRange | None = None

        try:
            result, date_range = await operation(oura)
        except OuraError as exc:
            await audit.record(
                AuditEvent(
                    action=AuditAction.TOOL_INVOKED,
                    account_id=oura.account_id,
                    correlation_id=get_correlation_id(),
                    status="error",
                    tool=tool,
                    resource=resource.value if resource else None,
                    start_date=date_range.start.isoformat() if date_range else None,
                    end_date=date_range.end.isoformat() if date_range else None,
                    duration_ms=_elapsed(started),
                    upstream_status=exc.upstream_status,
                    error_category=exc.category,
                    error_message=exc.message,
                )
            )
            # OuraError messages are written to be caller-safe: they never carry
            # tokens, headers or upstream response bodies.
            raise ToolError(exc.message) from exc

        await audit.record(
            AuditEvent(
                action=AuditAction.TOOL_INVOKED,
                account_id=oura.account_id,
                correlation_id=get_correlation_id(),
                status="partial" if isinstance(result, Summary) and result.unavailable else "ok",
                tool=tool,
                resource=resource.value if resource else None,
                start_date=date_range.start.isoformat() if date_range else None,
                end_date=date_range.end.isoformat() if date_range else None,
                duration_ms=_elapsed(started),
                cache=_cache_of(result),
                record_count=_count_of(result),
            )
        )
        return result, date_range

    async def records(
        self,
        tool: str,
        resource: Resource,
        start_date: str | None,
        end_date: str | None,
        *,
        default_days: int,
    ) -> RecordSet:
        """The common case: one Oura collection over a date range."""

        async def operation(service: OuraService):
            date_range = service.resolve_range(
                resource, start_date, end_date, default_days=default_days
            )
            return await service.get_records(resource, date_range), date_range

        fetch, date_range = await self.run(tool, operation, resource=resource)
        records = fetch.records if isinstance(fetch.records, list) else [fetch.records]
        return RecordSet(
            resource=resource.value,
            start_date=date_range.start,
            end_date=date_range.end,
            count=len(records),
            cache=fetch.cache.value,
            records=records,
        )


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _cache_of(result: Any) -> CacheStatus | None:
    return getattr(result, "cache", None) if isinstance(getattr(result, "cache", None), CacheStatus) else None


def _count_of(result: Any) -> int | None:
    records = getattr(result, "records", None)
    if isinstance(records, list):
        return len(records)
    days = getattr(result, "days", None)
    if isinstance(days, list):
        return len(days)
    return None
