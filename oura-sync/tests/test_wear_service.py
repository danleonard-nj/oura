"""The wear poller: fetching, persisting, transitions, failures and backfill."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.models.config import WearConfig
from app.models.errors import OuraAuthError, OuraRateLimitError, OuraUpstreamError
from app.models.resources import Resource
from app.models.wear import Confidence, WearStatus
from app.repositories.record_repository import RecordRepository
from app.repositories.wear_repository import WearRepository
from app.services.wear_service import WearService

#: The activity day of 2026-10-07 starts at 04:00 EDT, 08:00 UTC.
DAY_START = datetime(2026, 10, 7, 8, tzinfo=UTC)
SLOT = timedelta(minutes=5)


def at_slot(n: int) -> datetime:
    return DAY_START + SLOT * n


def activity(classes: str, day: str = "2026-10-07", offset: str = "-04:00") -> dict:
    return {
        "id": f"a-{day}",
        "day": day,
        "timestamp": f"{day}T04:00:00.000{offset}",
        "class_5_min": classes,
        "non_wear_time": classes.count("0") * 300,
    }


def pulse(when: datetime, bpm: int = 70) -> dict:
    return {"timestamp": when.isoformat().replace("+00:00", "Z"), "bpm": bpm, "source": "awake"}


class Fetch:
    def __init__(self, records) -> None:
        self.records = records
        self.cache = "bypass"


class StubOura:
    """Serves whatever activity and heart rate a test sets, or raises what it sets."""

    account_id = "default"

    def __init__(self) -> None:
        self.activity: list[dict] = []
        self.heart_rate: list[dict] = []
        self.errors: dict[Resource, Exception] = {}
        self.calls: list[tuple[Resource, date, date]] = []

    async def get_records(self, resource, date_range, *, use_cache: bool = True):
        assert use_cache is False
        self.calls.append((resource, date_range.start, date_range.end))
        if resource in self.errors:
            raise self.errors[resource]
        return Fetch(self.activity if resource is Resource.DAILY_ACTIVITY else self.heart_rate)


def service(database, oura, **config) -> WearService:
    return WearService(
        oura,  # type: ignore[arg-type]
        RecordRepository(database),
        WearRepository(database),
        WearConfig(**config),
    )


def transitions(database) -> list[tuple[str, datetime]]:
    return [(t["status"], t["at"]) for t in database["wear_transitions"].documents]


# ── Removal and replacement ───────────────────────────────────────────────


async def test_a_removal_and_a_replacement_are_each_recorded_once_at_evidence_time(database):
    oura = StubOura()
    wear = service(database, oura)

    oura.activity = [activity("1" * 100)]
    oura.heart_rate = [pulse(at_slot(99))]
    state = await wear.poll(now=at_slot(101))
    assert state.status is WearStatus.WORN

    oura.activity = [activity("1" * 100 + "0" * 8)]
    state = await wear.poll(now=at_slot(109))
    assert state.status is WearStatus.NOT_WORN
    assert state.since == at_slot(100)

    oura.activity = [activity("1" * 100 + "0" * 8 + "1" * 3)]
    state = await wear.poll(now=at_slot(112))
    assert state.status is WearStatus.WORN
    assert state.since == at_slot(108)

    assert transitions(database) == [
        ("WORN", DAY_START),
        ("NOT_WORN", at_slot(100)),
        ("WORN", at_slot(108)),
    ]


async def test_repeated_polls_on_the_same_evidence_change_nothing(database):
    oura = StubOura()
    oura.activity = [activity("1" * 100 + "0" * 8)]
    wear = service(database, oura)

    first = await wear.poll(now=at_slot(109))
    second = await wear.poll(now=at_slot(110))

    assert first.status is second.status is WearStatus.NOT_WORN
    assert transitions(database) == [("NOT_WORN", at_slot(100))]
    assert len(database["wear_days"].documents) == 1


async def test_a_flicker_through_unknown_does_not_record_a_transition(database):
    oura = StubOura()
    wear = service(database, oura)

    oura.activity = [activity("1" * 100)]
    await wear.poll(now=at_slot(100))
    oura.activity = [activity("1" * 100 + "0")]
    assert (await wear.poll(now=at_slot(101))).status is WearStatus.UNKNOWN
    oura.activity = [activity("1" * 100 + "0" + "1" * 2)]
    assert (await wear.poll(now=at_slot(103))).status is WearStatus.WORN

    assert transitions(database) == [("WORN", DAY_START)]


async def test_heart_rate_after_the_activity_data_puts_the_ring_back_on(database):
    oura = StubOura()
    oura.activity = [activity("1" * 100 + "0" * 12)]
    wear = service(database, oura)
    await wear.poll(now=at_slot(112))

    # Activity has not synced the new slots yet; heart rate already has.
    oura.heart_rate = [pulse(at_slot(114))]
    state = await wear.poll(now=at_slot(115))

    assert state.status is WearStatus.WORN
    assert state.since == at_slot(114)
    assert state.source == ["heartrate"]


# ── Midnight and the 04:00 activity day ───────────────────────────────────


async def test_a_removal_before_4am_carries_into_the_next_activity_day(database):
    oura = StubOura()
    # Off at 03:30 EDT on the 8th -- still the 7th's activity day -- and
    # still off at 04:30, now on the 8th's.
    oura.activity = [
        activity("1" * 282 + "0" * 6),
        activity("0" * 6, day="2026-10-08"),
    ]
    wear = service(database, oura)

    state = await wear.poll(now=datetime(2026, 10, 8, 8, 35, tzinfo=UTC))

    assert state.status is WearStatus.NOT_WORN
    assert state.since == at_slot(282)


async def test_the_activity_fetch_covers_tomorrow_because_end_date_is_exclusive(database):
    oura = StubOura()
    wear = service(database, oura, lookback_days=3)

    await wear.poll(now=datetime(2026, 10, 9, 23, 30, tzinfo=UTC))

    (activity_call,) = [c for c in oura.calls if c[0] is Resource.DAILY_ACTIVITY]
    assert activity_call[1:] == (date(2026, 10, 6), date(2026, 10, 11))


# ── Stale and delayed data ────────────────────────────────────────────────


async def test_a_ring_that_has_not_synced_reads_unknown(database):
    oura = StubOura()
    oura.activity = [activity("1" * 70)]
    wear = service(database, oura)

    state = await wear.poll(now=at_slot(70) + timedelta(hours=33))

    assert state.status is WearStatus.UNKNOWN
    assert state.fresh is False
    assert state.last_worn_at == at_slot(70)


async def test_a_stored_status_goes_unknown_when_read_after_it_goes_stale(database):
    oura = StubOura()
    oura.activity = [activity("1" * 100)]
    wear = service(database, oura)
    await wear.poll(now=at_slot(100))

    later = await wear.current(now=at_slot(100) + timedelta(hours=2))

    assert later.status is WearStatus.UNKNOWN
    assert later.polled_status is WearStatus.WORN
    assert later.since is None
    assert later.evidence_age_minutes == 120.0


async def test_a_shorter_response_never_overwrites_a_longer_stored_day(database):
    oura = StubOura()
    wear = service(database, oura)
    oura.activity = [activity("1" * 100 + "0" * 8)]
    await wear.poll(now=at_slot(108))

    # A partial or lagging response for the same day.
    oura.activity = [activity("1" * 90)]
    state = await wear.poll(now=at_slot(110))

    (stored,) = database["wear_days"].documents
    assert stored["slots"] == 108
    assert state.status is WearStatus.NOT_WORN


async def test_a_day_that_syncs_late_fills_in_the_history(database):
    oura = StubOura()
    wear = service(database, oura)
    oura.activity = [activity("1" * 70)]
    await wear.poll(now=at_slot(70))

    # The phone was away; the rest of the day lands hours later.
    oura.activity = [activity("1" * 70 + "0" * 100 + "1" * 118), activity("1" * 10, day="2026-10-08")]
    await wear.poll(now=datetime(2026, 10, 8, 8, 50, tzinfo=UTC))

    history = await wear.history(DAY_START, DAY_START + timedelta(days=1), now=datetime(2026, 10, 9, tzinfo=UTC))
    assert [(i.status, i.start, i.minutes) for i in history.intervals] == [
        (WearStatus.WORN, DAY_START, 350),
        (WearStatus.NOT_WORN, at_slot(70), 500),
        (WearStatus.WORN, at_slot(170), 590),
    ]
    assert history.not_worn_minutes == 500


# ── Failures ──────────────────────────────────────────────────────────────


async def test_an_activity_outage_judges_from_what_is_stored(database):
    oura = StubOura()
    wear = service(database, oura)
    oura.activity = [activity("1" * 100)]
    await wear.poll(now=at_slot(100))

    oura.errors[Resource.DAILY_ACTIVITY] = OuraUpstreamError("The Oura API returned 503.")
    oura.heart_rate = [pulse(at_slot(103))]
    state = await wear.poll(now=at_slot(104))

    assert state.status is WearStatus.WORN
    assert state.last_error == "daily_activity: The Oura API returned 503."
    assert state.heart_rate_through == at_slot(103)


async def test_a_heart_rate_failure_only_lowers_confidence(database):
    oura = StubOura()
    oura.activity = [activity("1" * 100 + "0" * 12)]
    oura.errors[Resource.HEARTRATE] = OuraRateLimitError("Oura rate limit reached.")
    wear = service(database, oura)

    state = await wear.poll(now=at_slot(112))

    assert state.status is WearStatus.NOT_WORN
    assert state.confidence is Confidence.MEDIUM
    assert state.source == ["daily_activity"]


async def test_an_auth_failure_with_nothing_stored_is_unknown(database):
    oura = StubOura()
    oura.errors = {r: OuraAuthError("The Oura access token was rejected.") for r in Resource}
    wear = service(database, oura)

    state = await wear.poll(now=at_slot(10))

    assert state.status is WearStatus.UNKNOWN
    assert "daily_activity" in state.last_error and "heartrate" in state.last_error


async def test_a_total_outage_keeps_the_last_judgement_until_it_goes_stale(database):
    oura = StubOura()
    wear = service(database, oura)
    oura.activity = [activity("1" * 100 + "0" * 8)]
    await wear.poll(now=at_slot(108))

    oura.errors = {r: OuraUpstreamError("down") for r in Resource}
    during = await wear.poll(now=at_slot(110))
    after = await wear.poll(now=at_slot(108) + timedelta(hours=2))

    assert during.status is WearStatus.NOT_WORN
    assert after.status is WearStatus.UNKNOWN
    assert transitions(database) == [("NOT_WORN", at_slot(100))]
    # The removal is still on record.
    assert (await wear.history(DAY_START, at_slot(108))).intervals[-1].status is WearStatus.NOT_WORN


async def test_a_failing_source_is_logged_once_not_every_poll(database, caplog):
    oura = StubOura()
    oura.errors[Resource.HEARTRATE] = OuraUpstreamError("down")
    wear = service(database, oura)

    for minute in (0, 15, 30):
        await wear.poll(now=DAY_START + timedelta(minutes=minute))

    warnings = [r for r in caplog.records if r.getMessage() == "wear poll could not read a source"]
    assert len(warnings) == 1


# ── History and backfill ──────────────────────────────────────────────────


async def test_history_absorbs_blips_and_clips_to_the_window(database):
    oura = StubOura()
    oura.activity = [activity("1" * 20 + "00" + "1" * 20 + "0" * 10 + "1" * 10)]
    wear = service(database, oura)
    await wear.poll(now=at_slot(62))

    history = await wear.history(at_slot(10), at_slot(60), now=at_slot(62))

    assert [(i.status, i.start, i.end) for i in history.intervals] == [
        (WearStatus.WORN, at_slot(10), at_slot(42)),
        (WearStatus.NOT_WORN, at_slot(42), at_slot(52)),
        (WearStatus.WORN, at_slot(52), at_slot(60)),
    ]
    assert history.covered_through == at_slot(60)


async def test_backfill_derives_wear_days_from_the_record_archive_without_calling_oura(database):
    archived = [activity("1" * 288, day=f"2026-09-{d:02d}") for d in (1, 2, 3)]
    records = database["oura_records"]
    for record in archived:
        records.documents.append(
            {
                "account_id": "default",
                "resource": "daily_activity",
                "record_key": record["id"],
                "observed_at": datetime.fromisoformat(record["timestamp"]),
                "payload": record,
            }
        )
    oura = StubOura()
    wear = service(database, oura)

    first = await wear.backfill(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC))
    again = await wear.backfill(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC))

    assert first == {"days": 3, "updated": 3}
    assert again == {"days": 3, "updated": 0}
    assert oura.calls == []
    history = await wear.history(
        datetime(2026, 9, 1, 8, tzinfo=UTC), datetime(2026, 9, 4, 8, tzinfo=UTC),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert [(i.status, i.minutes) for i in history.intervals] == [(WearStatus.WORN, 3 * 24 * 60)]
