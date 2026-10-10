"""Archive every record of a resource over a date range into `oura_records`.

The counterpart to `SnapshotService`: that one keeps a change log of
resources Oura reports only the current value of; this one copies the
records of everything else -- daily scores, sleep periods, workouts, heart
rate and battery samples -- so they outlive Oura's own retention.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time
from typing import Any

from app.models.config import OuraConfig, SyncConfig
from app.models.dates import DateRange
from app.models.errors import OuraTruncatedError
from app.models.resources import Resource, spec_for
from app.repositories.record_repository import RecordRepository
from app.services.oura_service import OuraService

#: Fields, in order of preference, that place a record in time.
_TIME_FIELDS = ("timestamp", "start_datetime", "bedtime_start", "day")


class RecordService:
    def __init__(
        self,
        oura: OuraService,
        repository: RecordRepository,
        config: SyncConfig,
        limits: OuraConfig,
    ) -> None:
        self._oura = oura
        self._repository = repository
        self._chunk_days = config.chunk_days
        self._max_heartrate_days = limits.max_heartrate_days

    def chunk_days(self, resource: Resource) -> int:
        """The widest window fetched for `resource` in one request."""
        days = self._chunk_days
        if (spec_max := spec_for(resource).max_days) is not None:
            days = min(days, spec_max)
        if resource is Resource.HEARTRATE:
            days = min(days, self._max_heartrate_days)
        return days

    async def archive(self, resource: Resource, date_range: DateRange) -> dict:
        """Fetch `resource` live over `date_range` and upsert every record.

        A range wider than `chunk_days` is fetched a window at a time. A
        failure part way leaves the earlier windows stored, which is safe:
        every write is an idempotent upsert, so retrying the range redoes
        nothing that already landed.

        Bypasses the response cache: a cached read could hide records that
        arrived since, which is exactly what a sync is for.
        """
        totals = {"fetched": 0, "inserted": 0, "updated": 0}
        for window in date_range.chunks(self.chunk_days(resource)):
            for key, value in (await self._archive_window(resource, window)).items():
                totals[key] += value
        return {
            "resource": resource.value,
            "start_date": date_range.start.isoformat(),
            "end_date": date_range.end.isoformat(),
            **totals,
        }

    async def _archive_window(self, resource: Resource, window: DateRange) -> dict[str, int]:
        try:
            fetch = await self._oura.get_records(resource, window, use_cache=False)
        except OuraTruncatedError:
            # Too many pages for one request: split until each half fits. A
            # single day that still truncates cannot be split further.
            if window.days == 1:
                raise
            first, second = window.halves()
            earlier = await self._archive_window(resource, first)
            later = await self._archive_window(resource, second)
            return {key: earlier[key] + later[key] for key in earlier}

        records = fetch.records if isinstance(fetch.records, list) else [fetch.records]

        prepared = [
            {
                "record_key": record_key(record),
                "observed_at": observed_at(record),
                "content_hash": content_hash(record),
                "payload": record,
            }
            for record in records
            if isinstance(record, dict)
        ]
        inserted, updated = await self._repository.upsert_many(
            self._oura.account_id, resource.value, prepared
        )
        return {"fetched": len(prepared), "inserted": inserted, "updated": updated}


def record_key(record: dict[str, Any]) -> str:
    """A key that is stable across polls for the same record.

    Oura's `id` where there is one. Time series samples (heart rate, battery)
    have none, so their timestamp -- plus the heart-rate `source`, since two
    sources can share a timestamp. Failing both, the content itself.
    """
    if record.get("id"):
        return str(record["id"])
    if record.get("timestamp"):
        source = record.get("source")
        return f"{record['timestamp']}|{source}" if source else str(record["timestamp"])
    if record.get("day"):
        return str(record["day"])
    return content_hash(record)


def observed_at(record: dict[str, Any]) -> datetime | None:
    """When the record happened, for range queries over the archive."""
    for field in _TIME_FIELDS:
        value = record.get(field)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(str(value)), time.min)
            except ValueError:
                continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def content_hash(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
