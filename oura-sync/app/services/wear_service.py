"""Polls whether the ring is being worn, and keeps the history of it.

Every `wear.poll_interval_minutes` (15 by default, run by `JobScheduler`)
it reads the last few
activity days and a few hours of heart rate from Oura, stores each day's
5-minute classification in `wear_days`, judges the current status from
everything stored (`wear_detection.assess`), and records changes between
WORN and NOT_WORN in `wear_transitions`.

**This is not live device status.** Oura's API only has what the ring last
synced through the phone, which can be minutes or hours ago. The judgement
is made at the newest evidence's time, and anything older than
`wear.max_evidence_age_minutes` reads UNKNOWN -- at poll time and again when
the status is read, so an outage cannot leave a stale WORN on display.

**Partial and failed polls.** Each source is fetched independently; one
failing only lowers confidence. Judgement always runs over the *stored*
days, which a shorter response never overwrites, plus the last heart-rate
time already known -- so a bad poll can add evidence but never take it
away, and a total outage simply re-judges the stored evidence until it goes
stale.

**History.** `history()` merges stored days into worn / not-worn intervals,
absorbing non-wear blips shorter than `wear.min_non_wear_minutes`. Each poll
re-reads `wear.lookback_days`, so a ring that syncs late fills in the days
it missed; `backfill()` derives older days from the record archive (which
`BackfillService` fills back to the first ring) without calling Oura.

**Activity days.** Oura's activity day runs 04:00 to 04:00 local time and
`daily_activity` treats `end_date` as exclusive (verified against the live
API), so the fetch asks for a day beyond tomorrow; slots are placed by each
day's own offset-carrying `timestamp`, so local midnight and the 04:00
boundary need no special handling.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.models.config import WearConfig
from app.models.dates import DateRange
from app.models.errors import OuraError
from app.services.history_service import parse_bound
from app.models.resources import Resource
from app.models.wear import (
    Confidence,
    WearHistory,
    WearInterval,
    WearState,
    WearStatus,
    WearTransition,
)
from app.repositories.record_repository import RecordRepository
from app.repositories.wear_repository import WearRepository
from app.services.oura_service import OuraService
from app.services.wear_detection import (
    ACTIVITY,
    HEART_RATE,
    activity_timeline,
    assess,
    parse_instant,
    smooth,
)
from app.utilities.logging import get_logger

logger = get_logger(__name__)

#: Activity days are 24 h (23 or 25 across DST); this much margin either side
#: of a window catches every day that overlaps it.
DAY_MARGIN = timedelta(days=2)
DEFINITE = (WearStatus.WORN, WearStatus.NOT_WORN)
#: The history window when `since` is not given.
DEFAULT_HISTORY_DAYS = 7


class WearService:
    def __init__(
        self,
        oura: OuraService,
        records: RecordRepository,
        repository: WearRepository,
        config: WearConfig,
    ) -> None:
        self._oura = oura
        self._records = records
        self._repository = repository
        self._account_id = oura.account_id
        self._lookback_days = config.lookback_days
        self._heart_rate_window = timedelta(hours=config.heart_rate_lookback_hours)
        self._max_age = timedelta(minutes=config.max_evidence_age_minutes)
        self._min_slots = config.min_non_wear_slots

    # ── Polling ───────────────────────────────────────────────────────────

    async def poll(self, now: datetime | None = None) -> WearState:
        """Fetch fresh evidence, store it, and update the judgement. Idempotent."""
        now = now or datetime.now(UTC)
        previous = await self._repository.get_status(self._account_id) or {}
        errors: dict[str, str] = {}

        try:
            for record in await self._fetch_activity(now.date()):
                await self._save_day(record)
        except OuraError as exc:
            errors[ACTIVITY] = exc.message

        heart_rate: list[datetime] = []
        try:
            heart_rate = await self._fetch_heart_rate(now)
        except OuraError as exc:
            errors[HEART_RATE] = exc.message

        # What a failed or thin heart-rate fetch cannot take away: the last
        # sample a previous poll saw is still evidence of wear.
        if known := _utc(previous.get("heart_rate_through")):
            heart_rate.append(known)

        days = await self._repository.days(
            self._account_id,
            since=now - timedelta(days=self._lookback_days) - DAY_MARGIN,
            until=now,
        )
        result = assess(
            activity_timeline(days),
            heart_rate,
            now,
            min_slots=self._min_slots,
            max_age=self._max_age,
            heart_rate_checked=HEART_RATE not in errors,
        )

        last_error = "; ".join(f"{source}: {message}" for source, message in errors.items()) or None
        self._log_errors(errors, previous.get("last_error"))

        fields: dict[str, Any] = {
            "status": result.status.value,
            "confidence": result.confidence.value,
            "reason": result.reason,
            "source": list(result.sources),
            "since": result.since,
            "last_worn_at": _latest(result.last_worn_at, _utc(previous.get("last_worn_at"))),
            "latest_evidence_at": result.latest_evidence_at,
            "activity_through": _latest(result.activity_through, _utc(previous.get("activity_through"))),
            "heart_rate_through": _latest(result.heart_rate_through, _utc(previous.get("heart_rate_through"))),
            "checked_at": now,
            "last_error": last_error,
        }

        if result.status is not WearStatus(previous.get("status", WearStatus.UNKNOWN)):
            logger.info(
                "wear status changed",
                extra={"from": previous.get("status"), "to": result.status.value, "reason": result.reason},
            )

        if result.status in DEFINITE and result.status.value != previous.get("definite_status"):
            at = result.since or result.latest_evidence_at
            fields["definite_status"] = result.status.value
            fields["definite_since"] = at
            new = await self._repository.record_transition(
                self._account_id,
                status=result.status.value,
                at=at,
                previous=previous.get("definite_status"),
                detected_at=now,
                confidence=result.confidence.value,
            )
            if new:
                logger.info(
                    "ring removed" if result.status is WearStatus.NOT_WORN else "ring put on",
                    extra={"at": at.isoformat(), "confidence": result.confidence.value},
                )

        await self._repository.set_status(self._account_id, **fields)
        return self._state(await self._repository.get_status(self._account_id), now)

    async def _fetch_activity(self, today: date) -> list[dict[str, Any]]:
        # `end_date` is exclusive for daily_activity, and a timezone ahead of
        # UTC is already on tomorrow's activity day -- hence today + 2.
        window = DateRange(start=today - timedelta(days=self._lookback_days), end=today + timedelta(days=2))
        fetch = await self._oura.get_records(Resource.DAILY_ACTIVITY, window, use_cache=False)
        records = fetch.records if isinstance(fetch.records, list) else [fetch.records]
        return [r for r in records if isinstance(r, dict)]

    async def _fetch_heart_rate(self, now: datetime) -> list[datetime]:
        since = now - self._heart_rate_window
        fetch = await self._oura.get_records(
            Resource.HEARTRATE, DateRange(start=since.date(), end=now.date()), use_cache=False
        )
        records = fetch.records if isinstance(fetch.records, list) else []
        samples = (parse_instant(r.get("timestamp")) for r in records if isinstance(r, dict))
        return [t for t in samples if t is not None and t >= since]

    async def _save_day(self, record: dict[str, Any]) -> bool:
        start = parse_instant(record.get("timestamp"))
        day = record.get("day")
        if start is None or not day:
            return False
        return await self._repository.save_day(
            self._account_id,
            day=str(day),
            start=start,
            class_5_min=record.get("class_5_min") or "",
            non_wear_seconds=record.get("non_wear_time"),
        )

    def _log_errors(self, errors: dict[str, str], previous_error: str | None) -> None:
        """A failure is logged once when it starts, not on every 15-minute poll."""
        for source, message in errors.items():
            if previous_error and f"{source}: {message}" in previous_error:
                logger.debug("wear poll source still failing", extra={"source": source})
            else:
                logger.warning("wear poll could not read a source", extra={"source": source, "error": message})
        if previous_error and not errors:
            logger.info("wear poll sources recovered")

    # ── Reading ───────────────────────────────────────────────────────────

    async def current(self, now: datetime | None = None) -> WearState:
        """The stored status, downgraded to UNKNOWN if its evidence has gone stale."""
        now = now or datetime.now(UTC)
        return self._state(await self._repository.get_status(self._account_id), now)

    async def history(self, since: datetime, until: datetime, now: datetime | None = None) -> WearHistory:
        """Worn / not-worn intervals and detected transitions within `since`..`until`."""
        until = min(until, now or datetime.now(UTC))
        days = await self._repository.days(self._account_id, since=since - DAY_MARGIN, until=until)
        runs = smooth(activity_timeline(days), self._min_slots)

        intervals = []
        for run in runs:
            start, end = max(run.start, since), min(run.end, until)
            if start < end:
                intervals.append(
                    WearInterval(
                        status=WearStatus.WORN if run.worn else WearStatus.NOT_WORN,
                        start=start,
                        end=end,
                        minutes=round((end - start).total_seconds() / 60),
                    )
                )

        transitions = [
            WearTransition(
                status=WearStatus(t["status"]),
                previous=WearStatus(t["previous"]) if t.get("previous") else None,
                at=_utc(t["at"]),
                detected_at=_utc(t["detected_at"]),
                confidence=Confidence(t["confidence"]),
            )
            for t in await self._repository.transitions(self._account_id, since=since, until=until)
        ]
        return WearHistory(
            start=since,
            end=until,
            covered_through=min(runs[-1].end, until) if runs else None,
            worn_minutes=sum(i.minutes for i in intervals if i.status is WearStatus.WORN),
            not_worn_minutes=sum(i.minutes for i in intervals if i.status is WearStatus.NOT_WORN),
            intervals=intervals,
            transitions=transitions,
        )

    async def backfill(self, since: datetime, until: datetime) -> dict[str, int]:
        """Derive wear days from archived `daily_activity` records. No Oura calls.

        Safe to repeat: a day is only rewritten when the archive covers more
        of it than is stored.
        """
        found = updated = 0
        cursor_until = until
        while True:
            # Newest first in pages, so a span of years is never one giant read.
            page = await self._records.recent(
                Resource.DAILY_ACTIVITY.value, account_id=self._account_id,
                since=since, until=cursor_until, limit=500,
            )
            for document in page:
                found += 1
                updated += await self._save_day(document.get("payload") or {})
            if len(page) < 500:
                break
            cursor_until = page[0]["observed_at"] - timedelta(microseconds=1)

        logger.info("wear backfill finished", extra={"days": found, "updated": updated})
        return {"days": found, "updated": updated}

    def _state(self, document: dict[str, Any] | None, now: datetime) -> WearState:
        if not document:
            return WearState(
                status=WearStatus.UNKNOWN, confidence=Confidence.LOW,
                reason="The wear status has not been polled yet.", source=[], fresh=False,
            )

        polled = WearStatus(document.get("status", WearStatus.UNKNOWN))
        latest = _utc(document.get("latest_evidence_at"))
        age = now - latest if latest else None
        fresh = age is not None and age <= self._max_age

        status, confidence, reason = polled, Confidence(document.get("confidence", "low")), document.get("reason", "")
        if not fresh:
            status, confidence = WearStatus.UNKNOWN, Confidence.LOW
            if polled is not WearStatus.UNKNOWN:
                reason = (
                    f"Stale: last judged {polled.value} on evidence "
                    f"{int(age.total_seconds() // 60) if age else '?'} minutes old; "
                    "the ring has not synced since."
                )

        return WearState(
            status=status,
            confidence=confidence,
            reason=reason,
            source=document.get("source") or [],
            since=_utc(document.get("since")) if status is not WearStatus.UNKNOWN else None,
            last_worn_at=_utc(document.get("last_worn_at")),
            latest_evidence_at=latest,
            activity_through=_utc(document.get("activity_through")),
            heart_rate_through=_utc(document.get("heart_rate_through")),
            evidence_age_minutes=round(age.total_seconds() / 60, 1) if age is not None else None,
            fresh=fresh,
            polled_status=polled,
            checked_at=_utc(document.get("checked_at")),
            last_error=document.get("last_error"),
        )


def history_window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    """Inclusive bounds from ISO dates or datetimes, defaulting to the last week."""
    upper = parse_bound(until, "until", end_of_day=True) or datetime.now(UTC)
    lower = parse_bound(since, "since", end_of_day=False) or upper - timedelta(days=DEFAULT_HISTORY_DAYS)
    if lower > upper:
        raise OuraError("since must not be after until.", status_code=400)
    return lower, upper


def _utc(value: Any) -> datetime | None:
    return parse_instant(value) if value is not None else None


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None
