"""The job scheduler: APScheduler for timing, a Redis claim per interval for exclusivity."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.models.config import OuraConfig, SyncConfig, WearConfig
from app.services.scheduler import JobScheduler
from tests.conftest import FakeRedis

OURA = OuraConfig(client_id="x", client_secret="y", redirect_uri="https://oura.test/cb")
MIDNIGHT = datetime(2026, 10, 9, tzinfo=UTC)


class Counting:
    """Stands in for the sync, back-sync and wear services; counts each job's runs."""

    def __init__(self, error: Exception | None = None) -> None:
        self.runs = {"sync": 0, "backfill": 0, "wear": 0}
        self.error = error

    async def _run(self, name: str) -> None:
        self.runs[name] += 1
        if self.error:
            raise self.error

    async def sync_all(self):
        await self._run("sync")

    async def run_logged(self):
        await self._run("backfill")

    async def poll(self):
        await self._run("wear")


def scheduler(redis, services, *, hours=6, backfill=True, minutes=15) -> JobScheduler:
    return JobScheduler(
        services, services, services, redis,  # type: ignore[arg-type]
        SyncConfig(interval_hours=hours, backfill_enabled=backfill),
        WearConfig(poll_interval_minutes=minutes),
        OURA,
    )


def test_every_job_is_on_by_default_and_each_can_be_turned_off():
    assert scheduler(FakeRedis(), Counting()).jobs == ["sync", "backfill", "wear"]
    assert scheduler(FakeRedis(), Counting(), hours=0, backfill=False, minutes=0).jobs == []


async def test_an_interval_runs_once_across_replicas_and_restarts():
    redis, services = FakeRedis(), Counting()
    at = MIDNIGHT + timedelta(minutes=3)

    assert await scheduler(redis, services).run_job("wear", now=at) is True
    # A second replica, or the same pod restarted, inside the same interval.
    assert await scheduler(redis, services).run_job("wear", now=at + timedelta(minutes=10)) is False
    assert services.runs["wear"] == 1


async def test_the_ring_check_runs_every_15_minutes():
    redis, services = FakeRedis(), Counting()
    jobs = scheduler(redis, services)

    ran = [
        await jobs.run_job("wear", now=MIDNIGHT + timedelta(minutes=minute))
        for minute in (0, 5, 14, 15, 29, 30, 45, 59, 60)
    ]

    assert ran == [True, False, False, True, False, True, True, False, True]


async def test_a_startup_run_does_not_block_the_next_scheduled_one():
    redis, services = FakeRedis(), Counting()
    jobs = scheduler(redis, services)

    # Started at 04:07; the 6-hourly sync is next due at 06:00, not 10:07.
    assert await jobs.run_job("sync", now=MIDNIGHT + timedelta(hours=4, minutes=7)) is True
    assert await jobs.run_job("sync", now=MIDNIGHT + timedelta(hours=6)) is True
    assert services.runs["sync"] == 2


async def test_without_redis_a_claimed_job_is_skipped_rather_than_risked_twice():
    services = Counting()

    assert await scheduler(FakeRedis(available=False), services).run_job("sync") is False
    assert services.runs["sync"] == 0


async def test_the_backfill_runs_unclaimed_because_it_holds_its_own_lease():
    redis, services = FakeRedis(), Counting()
    jobs = scheduler(redis, services)

    await jobs.run_job("backfill", now=MIDNIGHT)
    await jobs.run_job("backfill", now=MIDNIGHT)

    assert services.runs["backfill"] == 2
    assert redis.store == {}


async def test_a_failing_job_is_logged_and_does_not_escape(caplog):
    services = Counting(error=RuntimeError("boom"))

    assert await scheduler(FakeRedis(), services).run_job("wear") is True
    assert any(r.getMessage() == "scheduled job failed" for r in caplog.records)


async def test_starting_runs_every_job_now_and_then_on_aligned_boundaries():
    services = Counting()
    jobs = scheduler(FakeRedis(), services)

    jobs.start()
    try:
        for _ in range(100):
            if all(services.runs.values()):
                break
            await asyncio.sleep(0.01)
        triggers = {j.id: j.trigger for j in jobs._scheduler.get_jobs()}  # noqa: SLF001
    finally:
        jobs.shutdown()

    assert services.runs == {"sync": 1, "backfill": 1, "wear": 1}
    next_wear = triggers["wear"].get_next_fire_time(None, MIDNIGHT + timedelta(minutes=7))
    assert next_wear == MIDNIGHT + timedelta(minutes=15)
    next_sync = triggers["sync"].get_next_fire_time(None, MIDNIGHT + timedelta(hours=4))
    assert next_sync == MIDNIGHT + timedelta(hours=6)
