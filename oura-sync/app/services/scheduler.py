"""Periodic jobs inside the app, run by APScheduler.

| Job        | Every                             | Off when                       |
| ---------- | --------------------------------- | ------------------------------ |
| `sync`     | `sync.interval_hours` (6)         | `sync.interval_hours` is 0     |
| `backfill` | hour                              | `sync.backfill_enabled` false  |
| `wear`     | `wear.poll_interval_minutes` (15) | `wear.poll_interval_minutes` 0 |

APScheduler decides *when*; Redis decides *who*. Each job runs once at
startup and then on interval boundaries counted from the epoch, so every
replica ticks at the same instants -- the wear poll at :00, :15, :30 and
:45. Each boundary-to-boundary interval is claimed in Redis by the first
replica to reach it, so however many replicas run and however often a pod
restarts, a job runs at most once per interval. Without Redis a run is
skipped rather than risked twice. The back-sync is the exception: it holds
its own lease for the length of a walk (see `BackfillService`).

`coalesce` and `max_instances=1` mean a run that overruns its interval, or
a loop that stalls, never stacks runs up behind it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.models.config import OuraConfig, SyncConfig, WearConfig
from app.services.backfill_service import BackfillService
from app.services.sync_service import SyncService
from app.services.wear_service import WearService
from app.utilities.logging import get_logger, new_correlation_id, set_correlation_id

logger = get_logger(__name__)

#: Interval boundaries are counted from here, so they line up across replicas.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BACKFILL_INTERVAL_SECONDS = 60 * 60
#: A tick this late -- a stalled loop, a long GC -- still runs; later, it is skipped.
MISFIRE_GRACE_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    seconds: int
    run: Callable[[], Awaitable[object]]
    #: False when the job guards against concurrent runs itself.
    claimed: bool = True


class JobScheduler:
    def __init__(
        self,
        sync: SyncService,
        backfill: BackfillService,
        wear: WearService,
        redis: Redis,
        sync_config: SyncConfig,
        wear_config: WearConfig,
        oura: OuraConfig,
    ) -> None:
        self._redis = redis
        self._account_id = oura.account_id
        self._scheduler: AsyncIOScheduler | None = None

        jobs = []
        if sync_config.interval_hours > 0:
            jobs.append(Job("sync", int(sync_config.interval_hours * 3600), sync.sync_all))
        if sync_config.backfill_enabled:
            jobs.append(Job("backfill", BACKFILL_INTERVAL_SECONDS, backfill.run_logged, claimed=False))
        if wear_config.poll_interval_minutes > 0:
            jobs.append(Job("wear", int(wear_config.poll_interval_minutes * 60), wear.poll))
        self._jobs = {job.name: job for job in jobs}

    @property
    def jobs(self) -> list[str]:
        return list(self._jobs)

    def start(self) -> None:
        """Schedule every enabled job. Call from inside the serving event loop."""
        self._scheduler = AsyncIOScheduler(
            timezone=UTC,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": MISFIRE_GRACE_SECONDS},
        )
        now = datetime.now(UTC)
        for job in self._jobs.values():
            self._scheduler.add_job(
                self.run_job,
                IntervalTrigger(seconds=job.seconds, start_date=EPOCH, timezone=UTC),
                args=(job.name,),
                id=job.name,
                name=job.name,
                next_run_time=now,
            )
        self._scheduler.start()
        logger.info(
            "scheduler started",
            extra={
                "jobs": {
                    j.id: j.trigger.get_next_fire_time(None, now).isoformat()
                    for j in self._scheduler.get_jobs()
                }
            },
        )

    def shutdown(self) -> None:
        """Stop scheduling and cancel any run in progress."""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def run_job(self, name: str, now: datetime | None = None) -> bool:
        """Run `name` unless this interval already ran somewhere. Never raises.

        Returns whether it ran; a failure is logged and counts as having run,
        so the interval is not retried by every other replica.
        """
        job = self._jobs[name]
        if job.claimed and not await self._claim(job, now or datetime.now(UTC)):
            return False

        set_correlation_id(new_correlation_id())
        try:
            await job.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the schedule
            logger.error("scheduled job failed", extra={"job": name, "error": str(exc)})
        return True

    async def _claim(self, job: Job, now: datetime) -> bool:
        interval = int((now - EPOCH).total_seconds() // job.seconds)
        key = f"oura:{job.name}:schedule:{self._account_id}:{interval}"
        try:
            return bool(await self._redis.set(key, "1", nx=True, ex=job.seconds))
        except RedisError as exc:
            logger.warning("could not claim a scheduled run", extra={"job": job.name, "error": str(exc)})
            return False
