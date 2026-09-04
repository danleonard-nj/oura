"""MCP tool surface over the Oura API.

Every tool is thin: resolve the date range, call `OuraService`, return a typed
model. The shared `_run` helper owns timing, auditing and the translation of
`OuraError` into `ToolError`, so no individual tool repeats it.

Read-only by design. Nothing here writes to Oura.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from oura_mcp.infrastructure.logging import get_correlation_id, get_logger, new_correlation_id, set_correlation_id
from oura_mcp.mcp.context import ToolContext
from oura_mcp.models.audit import AuditAction, AuditEvent, CacheStatus
from oura_mcp.models.dates import DateRange
from oura_mcp.models.errors import OuraError
from oura_mcp.models.resources import Resource
from oura_mcp.models.results import Document, RecordSet, Summary
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.oura_service import OuraService
from oura_mcp.services.summary_service import SummaryService

logger = get_logger(__name__)

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

#: Resources reachable through `oura_query` but with no tool of their own.
#: Keeps the long tail available without another dozen schemas.
QUERYABLE = tuple(r.value for r in Resource)


def register_tools(server: MCPServer, context: ToolContext) -> None:
    """Attach every Oura tool to the MCP server."""

    async def _run(
        tool: str,
        operation: Callable[[OuraService], Awaitable[tuple[Any, DateRange | None]]],
        *,
        resource: Resource | None = None,
    ) -> Any:
        """Execute one tool body with timing, auditing and error translation.

        `operation` resolves its own date range and returns it alongside the
        result, so range validation happens inside this try block: a bad date
        reaches the model as its actual message rather than as the SDK's
        generic "Error executing tool".
        """
        if get_correlation_id() == "-":
            set_correlation_id(new_correlation_id())

        oura = context.resolve(OuraService)
        audit = context.resolve(AuditService)
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

    async def _records(
        tool: str,
        resource: Resource,
        start_date: str | None,
        end_date: str | None,
        *,
        default_days: int,
    ) -> RecordSet:
        async def operation(service: OuraService):
            date_range = service.resolve_range(
                resource, start_date, end_date, default_days=default_days
            )
            return await service.get_records(resource, date_range), date_range

        fetch, date_range = await _run(tool, operation, resource=resource)
        records = fetch.records if isinstance(fetch.records, list) else [fetch.records]
        return RecordSet(
            resource=resource.value,
            start_date=date_range.start,
            end_date=date_range.end,
            count=len(records),
            cache=fetch.cache.value,
            records=records,
        )

    # ── Composite ─────────────────────────────────────────────────────────

    @server.tool(
        name="oura_summary",
        title="Oura daily summary",
        description=(
            "Composite day-by-day view of the user's Oura data: sleep, readiness, activity, "
            "stress, resilience, blood oxygen and workouts, joined on the calendar day. "
            "Start here for questions like 'how did I sleep this week', 'how am I recovering' "
            "or 'compare my readiness to my activity'. Defaults to the last 7 days. Resources "
            "the account cannot access are listed in `unavailable` rather than failing the call."
        ),
    )
    async def oura_summary(start_date: StartDate = None, end_date: EndDate = None) -> Summary:
        async def operation(service: OuraService):
            date_range = service.resolve_range(
                Resource.DAILY_SLEEP, start_date, end_date, default_days=7
            )
            return await context.resolve(SummaryService).build(date_range), date_range

        summary, _ = await _run("oura_summary", operation)
        return summary

    # ── Daily scores ──────────────────────────────────────────────────────

    @server.tool(
        name="oura_sleep",
        title="Oura sleep",
        description=(
            "Daily sleep summaries and sleep scores: total and time in each stage, efficiency, "
            "latency, restfulness, average heart rate and HRV. One record per night. "
            "Defaults to the last 7 days. For the raw per-period sleep sessions behind these "
            "scores, use oura_query with resource='sleep'."
        ),
    )
    async def oura_sleep(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_sleep", Resource.DAILY_SLEEP, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_readiness",
        title="Oura readiness",
        description=(
            "Daily readiness scores and their contributors: resting heart rate, HRV balance, "
            "body temperature deviation, recovery index, sleep balance and activity balance. "
            "Use for 'am I recovered', 'should I train today' and illness-onset questions. "
            "Defaults to the last 7 days."
        ),
    )
    async def oura_readiness(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_readiness", Resource.DAILY_READINESS, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_activity",
        title="Oura activity",
        description=(
            "Daily activity summaries: steps, active and total calories, equivalent walking "
            "distance, MET minutes by intensity, sedentary and inactive time, and the activity "
            "score. Defaults to the last 7 days."
        ),
    )
    async def oura_activity(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_activity", Resource.DAILY_ACTIVITY, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_stress",
        title="Oura stress and recovery",
        description=(
            "Daily stress summaries: minutes classified as high stress versus recovery, and an "
            "overall day summary such as restored, normal or stressful. Defaults to the last "
            "7 days."
        ),
    )
    async def oura_stress(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_stress", Resource.DAILY_STRESS, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_spo2",
        title="Oura blood oxygen",
        description=(
            "Daily average blood oxygen saturation (SpO2) measured during sleep, plus the "
            "breathing disturbance index where available. Defaults to the last 7 days. "
            "Requires the spo2 scope and a ring generation that supports it."
        ),
    )
    async def oura_spo2(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_spo2", Resource.DAILY_SPO2, start_date, end_date, default_days=7
        )

    @server.tool(
        name="oura_resilience",
        title="Oura resilience",
        description=(
            "Daily resilience level (for example solid, strong, exceptional) with its sleep "
            "recovery, daytime recovery and stress contributors. A longer-horizon view of how "
            "well the user is absorbing load. Defaults to the last 14 days."
        ),
    )
    async def oura_resilience(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_resilience", Resource.DAILY_RESILIENCE, start_date, end_date, default_days=14
        )

    @server.tool(
        name="oura_cardiovascular_age",
        title="Oura cardiovascular age",
        description=(
            "Daily estimated vascular age relative to the user's chronological age. Changes "
            "very slowly, so a wide range is normal. Defaults to the last 30 days."
        ),
    )
    async def oura_cardiovascular_age(
        start_date: StartDate = None, end_date: EndDate = None
    ) -> RecordSet:
        return await _records(
            "oura_cardiovascular_age",
            Resource.DAILY_CARDIOVASCULAR_AGE,
            start_date,
            end_date,
            default_days=30,
        )

    @server.tool(
        name="oura_vo2_max",
        title="Oura VO2 max",
        description=(
            "Estimated VO2 max (cardiorespiratory fitness) readings. Produced only when the "
            "user completes a walking test, so records are sparse. Defaults to the last 90 days."
        ),
    )
    async def oura_vo2_max(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_vo2_max", Resource.VO2_MAX, start_date, end_date, default_days=90
        )

    # ── Time series and events ────────────────────────────────────────────

    @server.tool(
        name="oura_heart_rate",
        title="Oura heart rate time series",
        description=(
            "Individual heart rate samples with their timestamp and source (awake, rest, "
            "sleep, workout). This is high volume -- expect hundreds of samples per day -- so "
            "the range is capped at 14 days and defaults to 1 day. Prefer oura_sleep or "
            "oura_readiness for average and resting heart rate."
        ),
    )
    async def oura_heart_rate(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_heart_rate", Resource.HEARTRATE, start_date, end_date, default_days=1
        )

    @server.tool(
        name="oura_workouts",
        title="Oura workouts",
        description=(
            "Workouts, both auto-detected and manually entered: activity type, intensity, "
            "calories, distance, and start and end times. Defaults to the last 14 days."
        ),
    )
    async def oura_workouts(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_workouts", Resource.WORKOUT, start_date, end_date, default_days=14
        )

    @server.tool(
        name="oura_sessions",
        title="Oura sessions",
        description=(
            "Guided and unguided sessions such as meditation, breathing and naps, with their "
            "heart rate, HRV and motion series where recorded. Defaults to the last 14 days."
        ),
    )
    async def oura_sessions(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_sessions", Resource.SESSION, start_date, end_date, default_days=14
        )

    @server.tool(
        name="oura_tags",
        title="Oura tags",
        description=(
            "User-entered tags and notes -- caffeine, alcohol, illness, travel and so on -- "
            "useful for correlating behaviour with sleep and readiness. Returns Oura's enhanced "
            "tag format, which carries start and end times and comments. Defaults to the last "
            "14 days."
        ),
    )
    async def oura_tags(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_tags", Resource.ENHANCED_TAG, start_date, end_date, default_days=14
        )

    # ── Device and profile ────────────────────────────────────────────────

    @server.tool(
        name="oura_ring_info",
        title="Oura ring configuration",
        description=(
            "The user's ring hardware: model, design, colour, size, firmware version and setup "
            "date. Use when a question depends on ring generation, since some metrics are not "
            "available on older hardware. Defaults to the last 30 days of configuration records."
        ),
    )
    async def oura_ring_info(start_date: StartDate = None, end_date: EndDate = None) -> RecordSet:
        return await _records(
            "oura_ring_info", Resource.RING_CONFIGURATION, start_date, end_date, default_days=30
        )

    @server.tool(
        name="oura_personal_info",
        title="Oura personal info",
        description=(
            "The user's profile: age, biological sex, height and weight. Needed to interpret "
            "metrics that are relative to body composition. Takes no date range."
        ),
    )
    async def oura_personal_info() -> Document:
        async def operation(service: OuraService):
            return await service.get_personal_info(), None

        fetch, _ = await _run(
            "oura_personal_info", operation, resource=Resource.PERSONAL_INFO
        )
        return Document(
            resource=Resource.PERSONAL_INFO.value,
            cache=fetch.cache.value,
            document=fetch.records,
        )

    # ── Escape hatch ──────────────────────────────────────────────────────

    @server.tool(
        name="oura_query",
        title="Oura raw collection query",
        description=(
            "Fetch any Oura v2 collection by name. Use this only for collections the named "
            "tools do not cover: 'sleep' (raw per-period sleep sessions behind the daily "
            "score), 'sleep_time' (bedtime guidance), 'tag' (legacy tag format), "
            "'rest_mode_period' and 'ring_battery_level'. For everything else prefer the "
            "dedicated tool, which has a better-tuned default range."
        ),
    )
    async def oura_query(
        resource: Annotated[
            str,
            Field(description=f"Oura v2 collection name. One of: {', '.join(QUERYABLE)}."),
        ],
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> RecordSet:
        try:
            selected = Resource(resource)
        except ValueError as exc:
            raise ToolError(
                f"Unknown resource {resource!r}. Valid values: {', '.join(QUERYABLE)}."
            ) from exc

        if selected is Resource.PERSONAL_INFO:
            raise ToolError("Use oura_personal_info for personal_info; it takes no date range.")

        return await _records("oura_query", selected, start_date, end_date, default_days=7)


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
