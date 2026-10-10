"""Deciding whether the ring is worn, from evidence Oura has already synced.

Pure functions, no I/O -- `WearService` gathers the evidence and persists the
outcome. Two inputs:

* **`daily_activity.class_5_min`** -- one character per 5-minute slot of the
  activity day, which starts at 04:00 local time (`timestamp`, with its UTC
  offset): `0` non-wear, `1`-`5` rest through high activity. The string is
  published while the day is in progress and ends at the last slot the ring
  has synced, so its length says how fresh it is. Verified against the live
  API: `'0'` count x 300 s tracks `non_wear_time`, and `'0'` runs line up
  with gaps in heart rate.
* **Heart-rate samples** -- positive evidence only. The ring cannot read a
  pulse off a finger, but it does not sample continuously while worn either
  (gaps of an hour are normal in the day), so a missing sample never means
  the ring is off.

Rules, applied to the newest evidence:

* Nothing newer than `max_age` -> UNKNOWN. Oura is not a live feed; the ring
  syncs through the phone in the background, sometimes hours late.
* Heart rate newer than every classified slot -> WORN (newer evidence wins).
* Trailing worn slots -> WORN.
* Trailing non-wear run of at least `min_slots` -> NOT_WORN, unless heart
  rate was recorded inside it, which is a conflict -> UNKNOWN.
* A shorter trailing non-wear run -> UNKNOWN until it grows or ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.models.wear import Confidence, WearStatus

SLOT = timedelta(minutes=5)
#: Heart rate this close to the start of a non-wear run is the edge of the
#: slot the ring came off in, not a conflict.
EDGE_TOLERANCE = SLOT
#: Samples stamped this far past `now` are clock skew; anything later is ignored.
CLOCK_SKEW = timedelta(minutes=5)

ACTIVITY = "daily_activity"
HEART_RATE = "heartrate"


@dataclass(frozen=True, slots=True)
class Run:
    """Consecutive 5-minute slots with the same wear classification, in UTC."""

    worn: bool
    start: datetime
    end: datetime

    @property
    def slots(self) -> int:
        return round((self.end - self.start) / SLOT)


@dataclass(frozen=True, slots=True)
class Assessment:
    status: WearStatus
    confidence: Confidence
    reason: str
    sources: tuple[str, ...]
    since: datetime | None
    last_worn_at: datetime | None
    latest_evidence_at: datetime | None
    activity_through: datetime | None
    heart_rate_through: datetime | None


# ── Building the timeline ─────────────────────────────────────────────────


def parse_instant(value: Any) -> datetime | None:
    """An aware UTC datetime from an Oura timestamp, or None if it is not one."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def slot_runs(start: datetime, classes: str) -> list[Run]:
    """Collapse a `class_5_min` string starting at `start` into runs.

    A character outside 0-5 is treated as a hole in the data: it ends the
    run before it and belongs to no run itself.
    """
    runs: list[Run] = []
    for index, code in enumerate(classes):
        if code not in "012345":
            continue
        worn = code != "0"
        slot_start = start + SLOT * index
        if runs and runs[-1].worn == worn and runs[-1].end == slot_start:
            runs[-1] = Run(worn, runs[-1].start, slot_start + SLOT)
        else:
            runs.append(Run(worn, slot_start, slot_start + SLOT))
    return runs


def merge(runs: Iterable[Run]) -> list[Run]:
    """One continuous timeline from runs of several activity days.

    Adjacent runs of the same kind join, so a removal at 03:30 that carries
    on past the 04:00 activity-day boundary is one run. Overlaps should not
    happen -- each day's slots start where the last day's ended -- but if
    they do, the later run wins.
    """
    merged: list[Run] = []
    for run in sorted(runs, key=lambda r: r.start):
        if merged and run.start < merged[-1].end and merged[-1].worn != run.worn:
            previous = merged.pop()
            if run.start > previous.start:
                merged.append(Run(previous.worn, previous.start, run.start))
        if merged and merged[-1].worn == run.worn and run.start <= merged[-1].end:
            merged[-1] = Run(run.worn, merged[-1].start, max(merged[-1].end, run.end))
        else:
            merged.append(run)
    return merged


def smooth(runs: list[Run], min_slots: int) -> list[Run]:
    """Absorb non-wear runs shorter than `min_slots` between two worn runs.

    Those are sensor gaps or a quick hand-wash, not a removal. A short run at
    either end of the timeline is left alone: it may still be growing.
    """
    kept: list[Run] = []
    for index, run in enumerate(runs):
        bounded = (
            0 < index < len(runs) - 1
            and runs[index - 1].worn
            and runs[index + 1].worn
            and runs[index - 1].end == run.start
            and run.end == runs[index + 1].start
        )
        kept.append(Run(True, run.start, run.end) if not run.worn and run.slots < min_slots and bounded else run)
    return merge(kept)


def activity_timeline(days: Iterable[dict[str, Any]]) -> list[Run]:
    """Merged runs from stored or fetched activity days (`start` + `class_5_min`)."""
    runs: list[Run] = []
    for day in days:
        start = parse_instant(day.get("start") or day.get("timestamp"))
        if start is not None:
            runs.extend(slot_runs(start, day.get("class_5_min") or ""))
    return merge(runs)


# ── Judging the newest evidence ───────────────────────────────────────────


def assess(
    runs: list[Run],
    heart_rate: list[datetime],
    now: datetime,
    *,
    min_slots: int,
    max_age: timedelta,
    heart_rate_checked: bool = True,
) -> Assessment:
    """Judge the wear status at `now` from a merged activity timeline and heart rate.

    `heart_rate_checked` is False when heart rate could not be fetched this
    time. That only lowers confidence in NOT_WORN -- nobody looked for a pulse
    -- since heart rate is never the evidence for it.
    """
    samples = sorted(t for t in heart_rate if t <= now + CLOCK_SKEW)
    activity_through = runs[-1].end if runs else None
    heart_rate_through = samples[-1] if samples else None

    last_worn_run = next((r for r in reversed(runs) if r.worn), None)
    last_worn_at = _latest(last_worn_run.end if last_worn_run else None, heart_rate_through)
    latest = _latest(activity_through, heart_rate_through)

    def judged(status, confidence, reason, sources, since=None) -> Assessment:
        return Assessment(
            status, confidence, reason, tuple(sources), since,
            last_worn_at, latest, activity_through, heart_rate_through,
        )

    if latest is None:
        return judged(WearStatus.UNKNOWN, Confidence.LOW, "No activity or heart-rate data in the lookback window.", [])

    age = now - latest
    if age > max_age:
        return judged(
            WearStatus.UNKNOWN, Confidence.LOW,
            f"Stale: the newest evidence is {_minutes(age)} minutes old; the ring has not synced since.",
            [],
        )

    tail = runs[-1] if runs else None
    newer = [t for t in samples if activity_through is None or t > activity_through]

    if tail is None:
        return judged(
            WearStatus.WORN, Confidence.MEDIUM,
            "Recent heart rate, but no activity classification to corroborate it.",
            [HEART_RATE], newer[0],
        )

    if not tail.worn and newer:
        return judged(
            WearStatus.WORN, Confidence.MEDIUM,
            "Heart rate recorded after the last non-wear slot: the ring is back on.",
            [HEART_RATE], newer[0],
        )

    if tail.worn:
        corroborated = heart_rate_through is not None and heart_rate_through >= tail.start
        both_fresh = corroborated and now - activity_through <= max_age
        since = smooth(runs, min_slots)[-1].start
        return judged(
            WearStatus.WORN,
            Confidence.HIGH if both_fresh else Confidence.MEDIUM,
            "Recent activity slots classed as worn"
            + (", corroborated by heart rate." if corroborated else "."),
            [ACTIVITY, HEART_RATE] if corroborated else [ACTIVITY],
            since,
        )

    conflicting = [t for t in samples if tail.start + EDGE_TOLERANCE < t <= tail.end]
    if conflicting:
        return judged(
            WearStatus.UNKNOWN, Confidence.LOW,
            f"Conflict: {len(conflicting)} heart-rate sample(s) inside {tail.slots * 5} minutes "
            "classed as non-wear.",
            [ACTIVITY, HEART_RATE], tail.start,
        )

    if tail.slots < min_slots:
        return judged(
            WearStatus.UNKNOWN, Confidence.LOW,
            f"Possible removal: {tail.slots * 5} minutes of non-wear so far, under the "
            f"{min_slots * 5}-minute threshold.",
            [ACTIVITY], tail.start,
        )

    sources = [ACTIVITY, HEART_RATE] if heart_rate_checked else [ACTIVITY]
    confident = tail.slots >= 2 * min_slots and heart_rate_checked
    return judged(
        WearStatus.NOT_WORN,
        Confidence.HIGH if confident else Confidence.MEDIUM,
        f"{tail.slots * 5} consecutive minutes classed as non-wear, with no heart rate since.",
        sources,
        tail.start,
    )


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)
