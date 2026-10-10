"""Back-sync: walk each archived resource back through Oura's history.

The regular sync only reads forward from its first run. This fills in
everything before that, one window at a time, newest first:

* **Where it stops.** `sync.backfill_start` if configured; otherwise the
  setup date of the earliest ring in `ring_configuration`, since there is no
  ring data before a ring existed. With neither, it stops after
  `sync.backfill_empty_chunks` empty windows in a row, and never goes before
  `EARLIEST`.
* **Resumable.** Progress is saved to `sync_state` after every window, so a
  restart (or a rate limit) picks up where it left off, and a finished
  resource costs nothing on later checks.
* **Run once.** A Redis lease, refreshed after every window, keeps two
  replicas -- or the startup loop and a manual trigger -- from walking at
  the same time. If a lease ever lapses mid-window and a second walker
  starts, nothing breaks: every write is an idempotent upsert.

Current-state resources (ring configuration, profile) have no history behind
them in Oura's API, so there is nothing to back-sync for those.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.models.config import SyncConfig
from app.models.dates import DateRange, today_utc
from app.models.errors import OuraError, OuraRateLimitError
from app.models.resources import Resource, spec_for
from app.repositories.sync_state_repository import SyncStateRepository
from app.services.oura_service import OuraService
from app.services.record_service import RecordService
from app.utilities.logging import get_logger, new_correlation_id, set_correlation_id

logger = get_logger(__name__)

#: Before the first Oura ring shipped; the walk never goes further back.
EARLIEST = date(2015, 1, 1)
#: How long a claim on the walk lasts without being refreshed. Comfortably
#: longer than one window takes, retries and backoff included.
LEASE_SECONDS = 15 * 60


class BackfillService:
    def __init__(
        self,
        records: RecordService,
        oura: OuraService,
        state: SyncStateRepository,
        redis: Redis,
        config: SyncConfig,
    ) -> None:
        self._records = records
        self._oura = oura
        self._state = state
        self._redis = redis
        self._account_id = oura.account_id
        self._lease_key = f"oura:sync:backfill:{oura.account_id}"
        self._enabled = config.backfill_enabled
        self._configured_floor = config.backfill_start
        self._empty_limit = config.backfill_empty_chunks
        self._resources = [r for r in config.resources if not spec_for(r).current_state]
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def resources(self) -> list[Resource]:
        """The configured resources that have history to back-sync."""
        return list(self._resources)

    @property
    def running(self) -> bool:
        """Whether this process has a manually started walk in progress."""
        return self._task is not None and not self._task.done()

    # ── Status and control ────────────────────────────────────────────────

    async def status(self) -> list[dict]:
        states = {d["resource"]: d for d in await self._state.all(self._account_id)}
        return [
            {"resource": r.value, **(states.get(r.value, {}).get("backfill") or {"complete": False})}
            for r in self._resources
        ]

    async def reset(self, resources: list[Resource]) -> None:
        """Forget the progress for `resources`, so the next walk starts again from today."""
        for resource in resources:
            await self._state.set(self._account_id, resource.value, backfill=None)

    def start(self, resources: list[Resource] | None = None) -> bool:
        """Walk in the background. False if this process is already walking."""
        if self.running:
            return False
        self._task = asyncio.create_task(self.run_logged(resources))
        return True

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ── The walk ──────────────────────────────────────────────────────────

    async def run(self, resources: list[Resource] | None = None) -> list[dict] | None:
        """Walk every unfinished resource back to its floor.

        Returns one result per resource, or None if another walk holds the
        lease. A resource that fails is reported and left to resume next
        time; a rate limit ends the whole walk, since every resource draws on
        the same allowance.
        """
        owner = uuid4().hex
        try:
            claimed = await self._redis.set(self._lease_key, owner, nx=True, ex=LEASE_SECONDS)
        except RedisError as exc:
            logger.warning("could not claim the backfill", extra={"error": str(exc)})
            return None
        if not claimed:
            return None

        try:
            # Looked up only once some resource still has walking to do, so a
            # pass over finished resources makes no Oura calls at all.
            floor: list[date | None] = []

            async def resolved_floor() -> date | None:
                if not floor:
                    floor.append(await self._floor())
                return floor[0]

            results = []
            for resource in resources or self._resources:
                try:
                    results.append(await self._walk(resource, resolved_floor))
                except OuraRateLimitError as exc:
                    results.append({"resource": resource.value, "error": exc.message})
                    break
                except OuraError as exc:
                    results.append({"resource": resource.value, "error": exc.message})
            return results
        finally:
            with contextlib.suppress(RedisError):
                if await self._redis.get(self._lease_key) == owner:
                    await self._redis.delete(self._lease_key)

    async def _walk(
        self, resource: Resource, resolve_floor: Callable[[], Awaitable[date | None]]
    ) -> dict:
        progress = (await self._state.get(self._account_id, resource.value)).get("backfill") or {}
        if progress.get("complete"):
            return {"resource": resource.value, **progress}

        floor = await resolve_floor()
        # `oldest` is the earliest day already covered; the first window ends today.
        oldest = (
            date.fromisoformat(progress["oldest"])
            if progress.get("oldest")
            else today_utc() + timedelta(days=1)
        )
        stop = max(floor or EARLIEST, EARLIEST)
        chunk = self._records.chunk_days(resource)
        progress = {
            "oldest": progress.get("oldest"),
            "floor": floor.isoformat() if floor else None,
            "complete": False,
            "empty_chunks": progress.get("empty_chunks", 0),
            "fetched": progress.get("fetched", 0),
            "inserted": progress.get("inserted", 0),
            "last_error": None,
        }

        while not progress["complete"]:
            end = oldest - timedelta(days=1)
            if end < stop:
                progress["complete"] = True
                break

            window = DateRange(start=max(stop, end - timedelta(days=chunk - 1)), end=end)
            try:
                result = await self._records.archive(resource, window)
            except OuraError as exc:
                progress["last_error"] = exc.message
                await self._save(resource, progress)
                raise

            oldest = window.start
            progress["oldest"] = oldest.isoformat()
            progress["fetched"] += result["fetched"]
            progress["inserted"] += result["inserted"]
            progress["empty_chunks"] = 0 if result["fetched"] else progress["empty_chunks"] + 1
            progress["complete"] = window.start <= stop or (
                floor is None and progress["empty_chunks"] >= self._empty_limit
            )
            await self._save(resource, progress)
            await self._extend_lease()

        await self._save(resource, progress)
        logger.info(
            "backfill finished for resource",
            extra={"resource": resource.value, "oldest": progress["oldest"], "fetched": progress["fetched"]},
        )
        return {"resource": resource.value, **progress}

    async def _floor(self) -> date | None:
        """How far back to walk: configured, else the earliest ring's setup day."""
        if self._configured_floor is not None:
            return self._configured_floor

        try:
            fetch = await self._oura.get_records(
                Resource.RING_CONFIGURATION, DateRange(start=EARLIEST, end=today_utc()), use_cache=False
            )
        except OuraError as exc:
            logger.warning("could not read ring setup dates for the backfill", extra={"error": exc.message})
            return None

        rings = fetch.records if isinstance(fetch.records, list) else [fetch.records]
        setup_days = []
        for ring in rings:
            try:
                setup_days.append(datetime.fromisoformat(str(ring["set_up_at"])).date())
            except (KeyError, TypeError, ValueError):
                continue
        # A day early: the setup instant's UTC date can be a day after the
        # local day the first data belongs to.
        return min(setup_days) - timedelta(days=1) if setup_days else None

    async def _save(self, resource: Resource, progress: dict) -> None:
        await self._state.set(self._account_id, resource.value, backfill=dict(progress))

    async def _extend_lease(self) -> None:
        with contextlib.suppress(RedisError):
            await self._redis.expire(self._lease_key, LEASE_SECONDS)

    async def run_logged(self, resources: list[Resource] | None = None) -> None:
        """A walk that logs its outcome instead of raising -- for the scheduler and `start`."""
        set_correlation_id(new_correlation_id())
        try:
            results = await self.run(resources)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a background walk must not die silently
            logger.error("backfill failed", extra={"error": str(exc)})
            return
        if results is None:
            return
        logger.info(
            "backfill pass finished",
            extra={
                "resources": len(results),
                "complete": sum(1 for r in results if r.get("complete")),
                "failed": sum(1 for r in results if "error" in r),
            },
        )
