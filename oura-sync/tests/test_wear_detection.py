"""Wear detection rules, over hand-built activity days and heart-rate samples.

Activity days here look like Oura's: 04:00 local with its UTC offset, one
character per 5-minute slot, and the string ending at the last synced slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.wear import Confidence, WearStatus
from app.services.wear_detection import (
    ACTIVITY,
    HEART_RATE,
    SLOT,
    activity_timeline,
    assess,
    merge,
    slot_runs,
    smooth,
)

MAX_AGE = timedelta(minutes=60)
MIN_SLOTS = 3
EDT = "-04:00"


def day(date: str, classes: str, offset: str = EDT) -> dict:
    return {"day": date, "timestamp": f"{date}T04:00:00.000{offset}", "class_5_min": classes}


def end_of(record: dict) -> datetime:
    start = datetime.fromisoformat(record["timestamp"]).astimezone(UTC)
    return start + SLOT * len(record["class_5_min"])


def judge(days: list[dict], heart_rate: list[datetime] | None = None, now: datetime | None = None, **kwargs):
    runs = activity_timeline(days)
    now = now or (runs[-1].end if runs else datetime(2026, 10, 7, 12, tzinfo=UTC))
    return assess(runs, heart_rate or [], now, min_slots=MIN_SLOTS, max_age=MAX_AGE, **kwargs)


# ── The timeline ──────────────────────────────────────────────────────────


def test_slots_are_placed_from_the_days_own_offset():
    (run,) = slot_runs(datetime.fromisoformat("2026-10-07T04:00:00-04:00"), "111")

    assert run.start == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    assert run.end == datetime(2026, 10, 7, 8, 15, tzinfo=UTC)


def test_an_unknown_class_character_is_a_hole_not_a_classification():
    runs = slot_runs(datetime(2026, 10, 7, 8, tzinfo=UTC), "11x11")

    assert [(r.worn, r.slots) for r in runs] == [(True, 2), (True, 2)]
    assert runs[0].end < runs[1].start


def test_a_removal_spanning_the_4am_activity_day_boundary_is_one_run():
    days = [day("2026-10-07", "1" * 284 + "0000"), day("2026-10-08", "000000" + "1")]

    runs = activity_timeline(days)

    assert [(r.worn, r.slots) for r in runs] == [(True, 284), (False, 10), (True, 1)]


def test_a_timezone_ahead_of_utc_crosses_utc_midnight_cleanly():
    # 04:00 in Tokyo is 19:00 UTC the previous day.
    days = [day("2026-10-08", "1" * 70, "+09:00")]

    (run,) = activity_timeline(days)

    assert run.start == datetime(2026, 10, 7, 19, tzinfo=UTC)
    assert run.end == datetime(2026, 10, 8, 0, 50, tzinfo=UTC)


def test_a_25_hour_dst_day_runs_straight_into_the_next():
    # Fall back: 04:00 EDT to 04:00 EST is 25 hours, 300 slots.
    days = [day("2026-10-31", "1" * 300, "-04:00"), day("2026-11-01", "1" * 12, "-05:00")]

    (run,) = activity_timeline(days)

    assert run.slots == 312


def test_overlapping_days_let_the_later_one_win():
    start = datetime(2026, 10, 7, 8, tzinfo=UTC)
    runs = merge([*slot_runs(start, "1111"), *slot_runs(start + 2 * SLOT, "00")])

    assert [(r.worn, r.slots) for r in runs] == [(True, 2), (False, 2)]


def test_short_gaps_between_worn_runs_are_smoothed_but_trailing_ones_are_not():
    runs = activity_timeline([day("2026-10-07", "1111" + "00" + "1111" + "00")])

    smoothed = smooth(runs, MIN_SLOTS)

    assert [(r.worn, r.slots) for r in smoothed] == [(True, 10), (False, 2)]


# ── WORN ──────────────────────────────────────────────────────────────────


def test_worn_slots_corroborated_by_heart_rate_are_worn_with_high_confidence():
    record = day("2026-10-07", "1" * 100)
    result = judge([record], [end_of(record) - timedelta(minutes=10)])

    assert result.status is WearStatus.WORN
    assert result.confidence is Confidence.HIGH
    assert result.sources == (ACTIVITY, HEART_RATE)
    assert result.since == datetime(2026, 10, 7, 8, tzinfo=UTC)


def test_a_heart_rate_gap_alone_never_means_not_worn():
    # Hours of worn slots and not one heart-rate sample: still worn.
    record = day("2026-10-07", "2" * 100)

    result = judge([record], [])

    assert result.status is WearStatus.WORN
    assert result.confidence is Confidence.MEDIUM
    assert result.sources == (ACTIVITY,)


def test_a_brief_sensor_gap_does_not_reset_when_wearing_began():
    record = day("2026-10-07", "1" * 50 + "00" + "1" * 50)

    result = judge([record])

    assert result.status is WearStatus.WORN
    assert result.since == datetime(2026, 10, 7, 8, tzinfo=UTC)


def test_heart_rate_alone_is_worn_but_only_medium_confidence():
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)

    result = judge([], [now - timedelta(minutes=5)], now=now)

    assert result.status is WearStatus.WORN
    assert result.confidence is Confidence.MEDIUM
    assert result.sources == (HEART_RATE,)


# ── NOT_WORN ──────────────────────────────────────────────────────────────


def test_a_long_enough_non_wear_run_is_a_removal_at_its_first_slot():
    record = day("2026-10-07", "1" * 100 + "0" * 6)

    result = judge([record], [end_of(record) - timedelta(hours=1)])

    assert result.status is WearStatus.NOT_WORN
    assert result.confidence is Confidence.HIGH
    assert result.since == datetime(2026, 10, 7, 8, tzinfo=UTC) + SLOT * 100
    assert result.last_worn_at == result.since


def test_just_over_the_threshold_is_not_worn_with_medium_confidence():
    result = judge([day("2026-10-07", "1" * 100 + "0" * MIN_SLOTS)])

    assert result.status is WearStatus.NOT_WORN
    assert result.confidence is Confidence.MEDIUM


def test_without_a_heart_rate_check_a_removal_is_only_medium_confidence():
    result = judge([day("2026-10-07", "1" * 100 + "0" * 12)], heart_rate_checked=False)

    assert result.status is WearStatus.NOT_WORN
    assert result.confidence is Confidence.MEDIUM
    assert result.sources == (ACTIVITY,)


def test_heart_rate_in_the_slot_the_ring_came_off_is_not_a_conflict():
    record = day("2026-10-07", "1" * 100 + "0" * 6)
    removed = datetime(2026, 10, 7, 8, tzinfo=UTC) + SLOT * 100

    result = judge([record], [removed + timedelta(minutes=2)])

    assert result.status is WearStatus.NOT_WORN


# ── Put back on ───────────────────────────────────────────────────────────


def test_heart_rate_newer_than_a_removal_means_the_ring_is_back_on():
    record = day("2026-10-07", "1" * 100 + "0" * 12)
    back_on = end_of(record) + timedelta(minutes=7)

    result = judge([record], [back_on], now=back_on + timedelta(minutes=1))

    assert result.status is WearStatus.WORN
    assert result.since == back_on


def test_worn_slots_after_a_removal_are_a_replacement():
    record = day("2026-10-07", "1" * 100 + "0" * 12 + "1" * 4)

    result = judge([record])

    assert result.status is WearStatus.WORN
    assert result.since == datetime(2026, 10, 7, 8, tzinfo=UTC) + SLOT * 112


# ── UNKNOWN ───────────────────────────────────────────────────────────────


def test_a_short_trailing_non_wear_run_waits_for_confirmation():
    result = judge([day("2026-10-07", "1" * 100 + "00")])

    assert result.status is WearStatus.UNKNOWN
    assert "Possible removal" in result.reason


def test_heart_rate_inside_a_non_wear_run_is_a_conflict():
    record = day("2026-10-07", "1" * 100 + "0" * 12)
    removed = datetime(2026, 10, 7, 8, tzinfo=UTC) + SLOT * 100

    result = judge([record], [removed + timedelta(minutes=30)])

    assert result.status is WearStatus.UNKNOWN
    assert "Conflict" in result.reason


def test_stale_evidence_is_unknown_whatever_it_said():
    record = day("2026-10-07", "1" * 70)

    result = judge([record], now=end_of(record) + timedelta(hours=33))

    assert result.status is WearStatus.UNKNOWN
    assert "Stale" in result.reason
    # What was known is still reported.
    assert result.last_worn_at == end_of(record)
    assert result.latest_evidence_at == end_of(record)


def test_a_removal_on_stale_data_is_not_reported_as_not_worn():
    record = day("2026-10-07", "1" * 100 + "0" * 24)

    result = judge([record], now=end_of(record) + MAX_AGE + timedelta(minutes=1))

    assert result.status is WearStatus.UNKNOWN


def test_no_evidence_at_all_is_unknown():
    result = judge([], [])

    assert result.status is WearStatus.UNKNOWN
    assert result.latest_evidence_at is None


def test_samples_stamped_in_the_future_are_ignored():
    record = day("2026-10-07", "1" * 100 + "0" * 6)
    now = end_of(record)

    result = judge([record], [now + timedelta(hours=2)], now=now)

    assert result.status is WearStatus.NOT_WORN
