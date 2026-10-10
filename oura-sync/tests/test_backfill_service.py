"""The back-sync: walking archived resources back through Oura's history."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.config import OuraConfig, SyncConfig
from app.models.dates import DateRange, today_utc
from app.models.errors import OuraPermissionError, OuraRateLimitError
from app.models.resources import Resource
from app.repositories.record_repository import RecordRepository
from app.repositories.sync_state_repository import SyncStateRepository
from app.services.backfill_service import EARLIEST, BackfillService
from app.services.record_service import RecordService
from tests.conftest import FakeRedis

LIMITS = OuraConfig(client_id="x", client_secret="y", redirect_uri="https://oura.test/cb")


class Fetch:
    def __init__(self, records) -> None:
        self.records = records
        self.cache = "bypass"


class HistoricOura:
    """An Oura with real history: returns only the records inside each window."""

    account_id = "default"

    def __init__(self, records=None, rings=None, fail_on_call=None, error=None) -> None:
        self.records: dict[Resource, list[dict]] = records or {}
        self.rings = rings or []
        self.calls: list[tuple[Resource, DateRange]] = []
        self.fail_on_call = fail_on_call
        self.error = error or OuraPermissionError("refused")

    async def get_records(self, resource, date_range, *, use_cache: bool = True):
        assert use_cache is False
        self.calls.append((resource, date_range))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise self.error
        if resource is Resource.RING_CONFIGURATION:
            return Fetch(self.rings)
        return Fetch(
            [
                r for r in self.records.get(resource, [])
                if date_range.start <= date.fromisoformat(r["day"]) <= date_range.end
            ]
        )

    def windows(self, resource: Resource) -> list[DateRange]:
        return [w for r, w in self.calls if r is resource]


def days_ago(n: int) -> date:
    return today_utc() - timedelta(days=n)


def sleep_on(*days_back: int) -> list[dict]:
    return [{"id": f"s{n}", "day": days_ago(n).isoformat(), "score": 80} for n in days_back]


def backfill(database, oura, redis=None, **config) -> BackfillService:
    sync = SyncConfig(**{"resources": [Resource.DAILY_SLEEP], "chunk_days": 30, **config})
    records = RecordService(oura, RecordRepository(database), sync, LIMITS)  # type: ignore[arg-type]
    return BackfillService(
        records, oura, SyncStateRepository(database), redis or FakeRedis(), sync  # type: ignore[arg-type]
    )


async def progress(database, resource=Resource.DAILY_SLEEP) -> dict:
    return (await SyncStateRepository(database).get("default", resource.value)).get("backfill") or {}


# ── Walking back ──────────────────────────────────────────────────────────


async def test_it_walks_back_to_the_configured_start_newest_first(database):
    oura = HistoricOura(records={Resource.DAILY_SLEEP: sleep_on(0, 40, 89)})

    (result,) = await backfill(database, oura, backfill_start=days_ago(89)).run()

    windows = oura.windows(Resource.DAILY_SLEEP)
    assert windows[0].end == today_utc()
    assert windows[-1].start == days_ago(89)
    # Contiguous, no overlaps, no gaps, newest first.
    for newer, older in zip(windows, windows[1:]):
        assert older.end == newer.start - timedelta(days=1)
    assert result["complete"] is True
    assert result["fetched"] == 3
    assert len(database["oura_records"].documents) == 3


async def test_without_a_configured_start_it_stops_at_the_first_rings_setup(database):
    oura = HistoricOura(rings=[
        {"id": "r2", "set_up_at": f"{days_ago(20).isoformat()}T09:00:00+00:00"},
        {"id": "r1", "set_up_at": f"{days_ago(50).isoformat()}T09:00:00+00:00"},
    ])

    (result,) = await backfill(database, oura).run()

    assert result["complete"] is True
    # A day's margin before the earliest ring's setup.
    assert result["floor"] == days_ago(51).isoformat()
    assert oura.windows(Resource.DAILY_SLEEP)[-1].start == days_ago(51)


async def test_with_no_known_floor_it_stops_after_enough_empty_windows(database):
    oura = HistoricOura(records={Resource.DAILY_SLEEP: sleep_on(5)})

    (result,) = await backfill(database, oura, backfill_empty_chunks=3).run()

    # One window with data, then three empty ones.
    assert len(oura.windows(Resource.DAILY_SLEEP)) == 4
    assert result["complete"] is True
    assert result["floor"] is None


async def test_with_a_known_floor_empty_windows_do_not_stop_it_early(database):
    """Sparse data -- a rest mode period years ago -- must still be reached."""
    oura = HistoricOura(records={Resource.DAILY_SLEEP: sleep_on(300)})

    (result,) = await backfill(
        database, oura, backfill_start=days_ago(330), backfill_empty_chunks=1
    ).run()

    assert result["fetched"] == 1


async def test_it_never_walks_past_the_earliest_possible_day(database):
    oura = HistoricOura()

    (result,) = await backfill(database, oura, backfill_start=date(2000, 1, 1), chunk_days=365).run()

    assert oura.windows(Resource.DAILY_SLEEP)[-1].start == EARLIEST
    assert result["complete"] is True


async def test_current_state_resources_are_not_walked(database):
    service = backfill(
        database,
        HistoricOura(),
        resources=[Resource.PERSONAL_INFO, Resource.RING_CONFIGURATION, Resource.DAILY_SLEEP],
    )

    assert service.resources == [Resource.DAILY_SLEEP]


# ── Resuming ──────────────────────────────────────────────────────────────


async def test_a_failure_part_way_resumes_where_it_left_off(database):
    floor = days_ago(119)
    oura = HistoricOura(fail_on_call=3)

    (failed,) = await backfill(database, oura, backfill_start=floor).run()

    assert "error" in failed
    saved = await progress(database)
    assert saved["complete"] is False
    assert saved["last_error"] == "refused"
    assert saved["oldest"] == days_ago(59).isoformat()

    retry = HistoricOura()
    (result,) = await backfill(database, retry, backfill_start=floor).run()

    # Picks up below the last finished window rather than starting over.
    assert retry.windows(Resource.DAILY_SLEEP)[0].end == days_ago(60)
    assert result["complete"] is True
    assert result["last_error"] is None


async def test_a_finished_resource_costs_no_oura_calls(database):
    await backfill(database, HistoricOura(), backfill_start=days_ago(10)).run()

    again = HistoricOura()
    (result,) = await backfill(database, again).run()

    assert again.calls == []  # not even the ring lookup for the floor
    assert result["complete"] is True


async def test_reset_walks_again_from_today(database):
    service = backfill(database, HistoricOura(), backfill_start=days_ago(10))
    await service.run()

    await service.reset([Resource.DAILY_SLEEP])

    assert (await service.status())[0]["complete"] is False
    oura = HistoricOura()
    await backfill(database, oura, backfill_start=days_ago(10)).run()
    assert oura.windows(Resource.DAILY_SLEEP)[0].end == today_utc()


# ── Errors across resources ───────────────────────────────────────────────


async def test_a_refused_resource_does_not_stop_the_others(database):
    oura = HistoricOura(fail_on_call=1)

    results = await backfill(
        database, oura, resources=[Resource.DAILY_SPO2, Resource.DAILY_SLEEP],
        backfill_start=days_ago(10),
    ).run()

    assert "error" in results[0]
    assert results[1]["complete"] is True


async def test_a_rate_limit_ends_the_whole_pass(database):
    oura = HistoricOura(fail_on_call=1, error=OuraRateLimitError("slow down"))

    results = await backfill(
        database, oura, resources=[Resource.DAILY_SPO2, Resource.DAILY_SLEEP],
        backfill_start=days_ago(10),
    ).run()

    assert results == [{"resource": "daily_spo2", "error": "slow down"}]
    assert oura.windows(Resource.DAILY_SLEEP) == []


# ── One walker at a time ──────────────────────────────────────────────────


async def test_a_second_walker_backs_off_while_the_first_holds_the_lease(database):
    redis = FakeRedis()
    redis.store["oura:sync:backfill:default"] = "another-replica"

    assert await backfill(database, HistoricOura(), redis=redis).run() is None


async def test_the_lease_is_released_when_the_walk_ends(database):
    redis = FakeRedis()

    await backfill(database, HistoricOura(), redis=redis, backfill_start=days_ago(5)).run()

    assert "oura:sync:backfill:default" not in redis.store


async def test_an_unreachable_redis_skips_rather_than_risking_two_walkers(database):
    oura = HistoricOura()

    assert await backfill(database, oura, redis=FakeRedis(available=False)).run() is None
    assert oura.calls == []


async def test_start_walks_in_the_background_and_refuses_a_second_start(database):
    service = backfill(database, HistoricOura(), backfill_start=days_ago(5))

    assert service.start() is True
    assert service.start() is False
    await service._task

    assert service.running is False
    assert (await service.status())[0]["complete"] is True
    await service.aclose()
