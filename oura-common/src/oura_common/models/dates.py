"""Date and date-range resolution shared by every tool and route."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from oura_common.models.errors import DateRangeError


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        """Inclusive span, so a single day is 1."""
        return (self.end - self.start).days + 1

    def iter_days(self):
        for offset in range(self.days):
            yield self.start + timedelta(days=offset)

    def as_params(self) -> dict[str, str]:
        return {"start_date": self.start.isoformat(), "end_date": self.end.isoformat()}

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def today_utc() -> date:
    return datetime.now(UTC).date()


def parse_date(value: str | date | None, *, field: str) -> date | None:
    """Parse an ISO-8601 `YYYY-MM-DD` string into a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DateRangeError(
            f"{field} must be an ISO-8601 date (YYYY-MM-DD), got {text!r}"
        ) from exc


def resolve_range(
    start_date: str | date | None,
    end_date: str | date | None,
    *,
    default_days: int,
    max_days: int,
    allow_future: bool = False,
) -> DateRange:
    """Turn optional user input into a validated, bounded, inclusive range.

    Defaults: `end_date` is today; `start_date` is `default_days - 1` days
    before the end, so `default_days=1` means "just today".
    """
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    today = today_utc()

    if end is None:
        end = today if start is None else min(today, start + timedelta(days=default_days - 1))
    if start is None:
        start = end - timedelta(days=default_days - 1)

    if start > end:
        raise DateRangeError(
            f"start_date ({start.isoformat()}) must not be after end_date ({end.isoformat()})"
        )
    if not allow_future and start > today:
        raise DateRangeError(
            f"start_date ({start.isoformat()}) is in the future; Oura has no data yet"
        )

    span = (end - start).days + 1
    if span > max_days:
        raise DateRangeError(
            f"Requested range spans {span} days, which exceeds the {max_days}-day maximum "
            f"for this resource. Narrow the range or request it in batches."
        )
    return DateRange(start=start, end=end)


def is_recent(range_: DateRange, *, within_days: int = 2) -> bool:
    """True when the range touches the last few days.

    Recent Oura data is still being amended as the ring syncs, so it earns a
    short cache TTL; older data is settled and can be held much longer.
    """
    return (today_utc() - range_.end).days <= within_days
