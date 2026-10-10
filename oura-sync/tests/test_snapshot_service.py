"""The sync: change-log snapshots, the record archive, and the scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.config import OuraConfig, SyncConfig
from app.models.dates import DateRange, today_utc
from app.models.errors import OuraPermissionError, OuraTruncatedError
from app.models.resources import Resource
from app.repositories.record_repository import RecordRepository
from app.repositories.snapshot_repository import SnapshotRepository
from app.repositories.sync_state_repository import SyncStateRepository
from app.services.record_service import RecordService, observed_at, record_key
from app.services.snapshot_service import SnapshotService
from app.services.sync_service import SyncService

WINDOW = DateRange(start=date(2026, 9, 30), end=date(2026, 10, 1))

BATTERY = [
    {"timestamp": "2026-10-01T06:00:00+00:00", "level": 92, "charging": False},
    {"timestamp": "2026-10-01T06:05:00+00:00", "level": 91, "charging": False},
    {"timestamp": "2026-10-01T06:10:00+00:00", "level": 90, "charging": False},
]


class Fetch:
    def __init__(self, records) -> None:
        self.records = records
        self.cache = "bypass"


class FakeOuraService:
    """Stands in for OuraService -- only what the sync calls."""

    account_id = "default"

    def __init__(self, personal_info=None, records=None, failing=(), truncate_over_days=None) -> None:
        self._personal_info = personal_info or {}
        self.records: dict[Resource, list] = records or {}
        self.failing = set(failing)
        # Windows wider than this raise OuraTruncatedError, like a range
        # needing more than MAX_PAGES would.
        self.truncate_over_days = truncate_over_days
        self.calls: list[tuple[Resource, DateRange]] = []

    async def get_personal_info(self, *, use_cache: bool = True) -> Fetch:
        return Fetch(self._personal_info)

    async def get_records(self, resource, date_range, *, use_cache: bool = True) -> Fetch:
        assert use_cache is False, "a sync must read live, not from the cache"
        self.calls.append((resource, date_range))
        if resource in self.failing:
            raise OuraPermissionError(f"{resource.value} is not granted")
        if self.truncate_over_days is not None and date_range.days > self.truncate_over_days:
            raise OuraTruncatedError("too many pages")
        return Fetch(self.records.get(resource, []))


def snapshots(database, oura) -> SnapshotService:
    return SnapshotService(oura, SnapshotRepository(database))  # type: ignore[arg-type]


LIMITS = OuraConfig(client_id="x", client_secret="y", redirect_uri="https://oura.test/cb")


def archive(database, oura, **config) -> RecordService:
    return RecordService(oura, RecordRepository(database), SyncConfig(**config), LIMITS)  # type: ignore[arg-type]


def sync(database, oura, **config) -> SyncService:
    return SyncService(
        snapshots(database, oura),
        archive(database, oura, **config),
        SyncStateRepository(database),  # type: ignore[arg-type]
        SyncConfig(**config),
        LIMITS,
    )


def counts(result: dict) -> dict:
    return {k: result[k] for k in ("resource", "fetched", "inserted", "updated")}


# ── Change log (current-state resources) ──────────────────────────────────


async def test_a_new_resource_is_stored(database):
    service = snapshots(database, FakeOuraService(personal_info={"weight": 78.0}))

    result = await service.sync_one(Resource.PERSONAL_INFO, WINDOW)

    assert result == {"resource": "personal_info", "changed": True}
    assert len(database["resource_snapshots"].documents) == 1


async def test_an_identical_poll_does_not_add_a_document(database):
    service = snapshots(database, FakeOuraService(personal_info={"weight": 78.0}))

    await service.sync_one(Resource.PERSONAL_INFO, WINDOW)
    result = await service.sync_one(Resource.PERSONAL_INFO, WINDOW)

    assert result == {"resource": "personal_info", "changed": False}
    assert len(database["resource_snapshots"].documents) == 1


async def test_an_identical_poll_still_bumps_last_seen_at(database):
    service = snapshots(database, FakeOuraService(personal_info={"weight": 78.0}))

    await service.sync_one(Resource.PERSONAL_INFO, WINDOW)
    first_seen = database["resource_snapshots"].documents[0]["last_seen_at"]
    await service.sync_one(Resource.PERSONAL_INFO, WINDOW)

    assert database["resource_snapshots"].documents[0]["last_seen_at"] >= first_seen


async def test_a_changed_value_adds_a_second_document(database):
    oura = FakeOuraService(personal_info={"weight": 78.0})
    service = snapshots(database, oura)
    await service.sync_one(Resource.PERSONAL_INFO, WINDOW)

    oura._personal_info = {"weight": 79.5}
    result = await service.sync_one(Resource.PERSONAL_INFO, WINDOW)

    assert result == {"resource": "personal_info", "changed": True}
    assert len(database["resource_snapshots"].documents) == 2


async def test_ring_configuration_snapshots_every_ring_in_the_window(database):
    """A firmware update changes a field on an existing record, so the whole
    list is the value whose changes are logged."""
    rings = [{"id": "r1", "firmware_version": "2.9.1"}, {"id": "r2", "firmware_version": "1.0"}]
    oura = FakeOuraService(records={Resource.RING_CONFIGURATION: rings})

    await snapshots(database, oura).sync_one(Resource.RING_CONFIGURATION, WINDOW)

    assert database["resource_snapshots"].documents[0]["payload"] == rings


async def test_no_records_in_the_window_stores_nothing(database):
    result = await snapshots(database, FakeOuraService()).sync_one(Resource.RING_CONFIGURATION, WINDOW)

    assert result["changed"] is False
    assert "reason" in result
    assert database["resource_snapshots"].documents == []


# ── Record archive ────────────────────────────────────────────────────────


async def test_every_battery_sample_is_archived(database):
    """Not just the latest: Oura forgets these, so each one is kept."""
    oura = FakeOuraService(records={Resource.RING_BATTERY_LEVEL: BATTERY})

    result = await archive(database, oura).archive(Resource.RING_BATTERY_LEVEL, WINDOW)

    assert counts(result) == {"resource": "ring_battery_level", "fetched": 3, "inserted": 3, "updated": 0}
    assert (result["start_date"], result["end_date"]) == ("2026-09-30", "2026-10-01")
    stored = database["oura_records"].documents
    assert [d["payload"]["level"] for d in stored] == [92, 91, 90]
    assert stored[0]["observed_at"] == datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


async def test_overlapping_windows_do_not_duplicate_records(database):
    oura = FakeOuraService(records={Resource.RING_BATTERY_LEVEL: BATTERY})
    service = archive(database, oura)
    await service.archive(Resource.RING_BATTERY_LEVEL, WINDOW)

    oura.records[Resource.RING_BATTERY_LEVEL] = BATTERY + [
        {"timestamp": "2026-10-01T06:15:00+00:00", "level": 89, "charging": False}
    ]
    result = await service.archive(Resource.RING_BATTERY_LEVEL, WINDOW)

    assert result["inserted"] == 1
    assert result["updated"] == 0
    assert len(database["oura_records"].documents) == 4


async def test_a_revised_record_is_updated_in_place(database):
    """Oura finalises a sleep score after the fact; the archive follows."""
    oura = FakeOuraService(records={Resource.DAILY_SLEEP: [{"id": "s1", "day": "2026-10-01", "score": 70}]})
    service = archive(database, oura)
    await service.archive(Resource.DAILY_SLEEP, WINDOW)

    oura.records[Resource.DAILY_SLEEP] = [{"id": "s1", "day": "2026-10-01", "score": 74}]
    result = await service.archive(Resource.DAILY_SLEEP, WINDOW)

    assert result["updated"] == 1
    assert len(database["oura_records"].documents) == 1
    assert database["oura_records"].documents[0]["payload"]["score"] == 74


async def test_a_wide_range_is_fetched_a_window_at_a_time(database):
    oura = FakeOuraService()

    await archive(database, oura, chunk_days=30).archive(
        Resource.DAILY_SLEEP, DateRange(start=date(2026, 1, 1), end=date(2026, 3, 31))
    )

    windows = [window for _, window in oura.calls]
    assert [w.days for w in windows] == [30, 30, 30]
    assert windows[0].start == date(2026, 1, 1) and windows[-1].end == date(2026, 3, 31)


async def test_heart_rate_windows_never_exceed_its_cap(database):
    oura = FakeOuraService()

    await archive(database, oura, chunk_days=90).archive(
        Resource.HEARTRATE, DateRange(start=date(2026, 1, 1), end=date(2026, 1, 31))
    )

    assert max(window.days for _, window in oura.calls) == 14


async def test_a_truncated_window_is_split_until_it_fits(database):
    oura = FakeOuraService(
        records={Resource.HEARTRATE: [{"timestamp": "2026-01-01T00:00:00+00:00", "bpm": 60}]},
        truncate_over_days=3,
    )

    result = await archive(database, oura).archive(
        Resource.HEARTRATE, DateRange(start=date(2026, 1, 1), end=date(2026, 1, 14))
    )

    fetched_windows = [w for _, w in oura.calls if w.days <= 3]
    # Every day covered exactly once by the windows that succeeded.
    covered = sorted(day for w in fetched_windows for day in w.iter_days())
    assert covered == list(DateRange(start=date(2026, 1, 1), end=date(2026, 1, 14)).iter_days())
    assert result["fetched"] == len(fetched_windows)


async def test_a_single_day_that_still_truncates_is_an_error(database):
    oura = FakeOuraService(truncate_over_days=0)

    with pytest.raises(OuraTruncatedError):
        await archive(database, oura).archive(Resource.HEARTRATE, WINDOW)


def test_record_keys_are_stable_and_distinct():
    assert record_key({"id": "abc", "day": "2026-10-01"}) == "abc"
    # Heart rate samples from two sources can share a timestamp.
    assert record_key({"timestamp": "2026-10-01T06:00:00+00:00", "source": "rest"}) != record_key(
        {"timestamp": "2026-10-01T06:00:00+00:00", "source": "awake"}
    )
    assert record_key({"day": "2026-10-01"}) == "2026-10-01"


def test_observed_at_falls_back_to_the_day():
    assert observed_at({"day": "2026-10-01"}) == datetime(2026, 10, 1, tzinfo=UTC)
    assert observed_at({"start_datetime": "2026-10-01T07:30:00+02:00"}) == datetime(
        2026, 10, 1, 5, 30, tzinfo=UTC
    )
    assert observed_at({"score": 80}) is None


# ── Sync run ──────────────────────────────────────────────────────────────


async def test_a_run_covers_every_resource_by_default(database):
    oura = FakeOuraService(records={Resource.RING_BATTERY_LEVEL: BATTERY})

    results = await sync(database, oura).sync_all()

    assert {r["resource"] for r in results} == {r.value for r in Resource}


async def test_current_state_resources_go_to_the_change_log_and_the_rest_to_the_archive(database):
    oura = FakeOuraService(
        personal_info={"weight": 78.0},
        records={
            Resource.RING_CONFIGURATION: [{"id": "r1", "firmware_version": "2.9.1"}],
            Resource.RING_BATTERY_LEVEL: BATTERY,
        },
    )

    await sync(database, oura).sync_all(
        [Resource.PERSONAL_INFO, Resource.RING_CONFIGURATION, Resource.RING_BATTERY_LEVEL]
    )

    assert {d["resource"] for d in database["resource_snapshots"].documents} == {
        "personal_info",
        "ring_configuration",
    }
    assert {d["resource"] for d in database["oura_records"].documents} == {"ring_battery_level"}


async def test_the_window_is_the_lookback_ending_today(database):
    oura = FakeOuraService()

    await sync(database, oura, lookback_days=3).sync_all([Resource.DAILY_SLEEP])

    (_, window), = oura.calls
    assert window.days == 3


async def test_one_failing_resource_does_not_stop_the_rest(database):
    oura = FakeOuraService(
        records={Resource.RING_BATTERY_LEVEL: BATTERY}, failing={Resource.DAILY_SPO2}
    )

    results = await sync(database, oura).sync_all([Resource.DAILY_SPO2, Resource.RING_BATTERY_LEVEL])

    assert "error" in results[0]
    assert results[1]["inserted"] == 3


async def test_a_run_after_an_outage_closes_the_gap(database):
    """The lookback alone would skip every day the service was down."""
    oura = FakeOuraService()
    service = sync(database, oura, lookback_days=2)
    await SyncStateRepository(database).set(
        "default", "daily_sleep", synced_through=(today_utc() - timedelta(days=10)).isoformat()
    )

    await service.sync_all([Resource.DAILY_SLEEP])

    (_, window), = oura.calls
    assert window.start == today_utc() - timedelta(days=10)
    assert window.end == today_utc()


async def test_a_successful_run_records_how_far_it_got(database):
    await sync(database, FakeOuraService()).sync_all([Resource.DAILY_SLEEP])

    state = await SyncStateRepository(database).get("default", "daily_sleep")
    assert state["synced_through"] == today_utc().isoformat()


async def test_a_failed_run_does_not_advance_the_cursor(database):
    await sync(database, FakeOuraService(failing={Resource.DAILY_SPO2})).sync_all([Resource.DAILY_SPO2])

    assert await SyncStateRepository(database).get("default", "daily_spo2") == {}
