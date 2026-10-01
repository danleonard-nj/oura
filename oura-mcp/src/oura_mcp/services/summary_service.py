"""Composite day-by-day view assembled from several Oura collections."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from oura_common.infrastructure.logging import get_logger
from oura_common.models.dates import DateRange, parse_date
from oura_common.models.errors import OuraError, OuraPermissionError
from oura_common.models.resources import Resource
from oura_common.services.oura_service import OuraService

from oura_mcp.models.results import DaySummary, Summary

logger = get_logger(__name__)

#: Resource -> the DaySummary field it populates. Every one of these is keyed
#: by Oura's `day` field, which is what makes the join possible.
DAILY_FIELDS: dict[Resource, str] = {
    Resource.DAILY_SLEEP: "sleep",
    Resource.DAILY_READINESS: "readiness",
    Resource.DAILY_ACTIVITY: "activity",
    Resource.DAILY_STRESS: "stress",
    Resource.DAILY_RESILIENCE: "resilience",
    Resource.DAILY_SPO2: "spo2",
}


class SummaryService:
    """Fans out across the daily collections and joins them on `day`.

    Resources are fetched concurrently. A resource the account cannot see --
    an ungranted scope, or a metric behind an Oura subscription -- is reported
    in `unavailable` rather than failing the whole summary, because a partial
    picture is far more useful here than an error.
    """

    def __init__(self, oura: OuraService) -> None:
        self._oura = oura

    async def build(self, date_range: DateRange, *, use_cache: bool = True) -> Summary:
        resources = [*DAILY_FIELDS, Resource.WORKOUT]

        results = await asyncio.gather(
            *(
                self._oura.get_records(resource, date_range, use_cache=use_cache)
                for resource in resources
            ),
            return_exceptions=True,
        )

        days: dict[date, DaySummary] = {}
        unavailable: list[str] = []

        for resource, result in zip(resources, results, strict=True):
            if isinstance(result, BaseException):
                unavailable.append(f"{resource.value}: {_reason(result, resource)}")
                continue

            records = result.records if isinstance(result.records, list) else []
            if resource is Resource.WORKOUT:
                for record in records:
                    day = _day_of(record)
                    if day is not None:
                        _slot(days, day).workouts.append(record)
                continue

            field = DAILY_FIELDS[resource]
            for record in records:
                day = _day_of(record)
                if day is not None:
                    setattr(_slot(days, day), field, record)

        return Summary(
            start_date=date_range.start,
            end_date=date_range.end,
            days=[days[key] for key in sorted(days)],
            unavailable=unavailable,
        )


def _slot(days: dict[date, DaySummary], day: date) -> DaySummary:
    if day not in days:
        days[day] = DaySummary(day=day)
    return days[day]


def _day_of(record: dict[str, Any]) -> date | None:
    """Read the `day` a record belongs to, tolerating records without one."""
    try:
        return parse_date(record.get("day"), field="day")
    except Exception:  # noqa: BLE001 - a malformed record should not sink the summary
        return None


def _reason(error: BaseException, resource: Resource) -> str:
    if isinstance(error, OuraPermissionError):
        return "not permitted for this account (scope or subscription)"
    if isinstance(error, OuraError):
        return error.message
    logger.warning(
        "summary fan-out failed",
        exc_info=error,
        extra={"resource": resource.value},
    )
    return "unavailable"
