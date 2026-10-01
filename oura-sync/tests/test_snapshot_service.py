"""SnapshotService: only a genuine change produces a new document."""

from __future__ import annotations

from app.models.resources import Resource

from app.repositories.snapshot_repository import SnapshotRepository
from app.services.snapshot_service import SnapshotService


class Fetch:
    def __init__(self, records) -> None:
        self.records = records
        self.cache = "bypass"


class FakeOuraService:
    """Stands in for OuraService -- only what SnapshotService calls."""

    account_id = "default"

    def __init__(self, personal_info=None, records=None) -> None:
        self._personal_info = personal_info or {}
        self._records = records if records is not None else []

    async def get_personal_info(self, *, use_cache: bool = True) -> Fetch:
        return Fetch(self._personal_info)

    async def get_records(self, resource, date_range, *, use_cache: bool = True) -> Fetch:
        return Fetch(self._records)


def build(database, oura: FakeOuraService) -> SnapshotService:
    return SnapshotService(oura, SnapshotRepository(database))  # type: ignore[arg-type]


async def test_a_new_resource_is_stored(database):
    oura = FakeOuraService(personal_info={"weight": 78.0})
    service = build(database, oura)

    result = await service.sync_one(Resource.PERSONAL_INFO)

    assert result == {"resource": "personal_info", "changed": True}
    assert len(database["resource_snapshots"].documents) == 1


async def test_an_identical_poll_does_not_add_a_document(database):
    oura = FakeOuraService(personal_info={"weight": 78.0})
    service = build(database, oura)

    await service.sync_one(Resource.PERSONAL_INFO)
    result = await service.sync_one(Resource.PERSONAL_INFO)

    assert result == {"resource": "personal_info", "changed": False}
    assert len(database["resource_snapshots"].documents) == 1


async def test_an_identical_poll_still_bumps_last_seen_at(database):
    oura = FakeOuraService(personal_info={"weight": 78.0})
    service = build(database, oura)

    await service.sync_one(Resource.PERSONAL_INFO)
    first_seen = database["resource_snapshots"].documents[0]["last_seen_at"]

    await service.sync_one(Resource.PERSONAL_INFO)
    second_seen = database["resource_snapshots"].documents[0]["last_seen_at"]

    assert second_seen >= first_seen


async def test_a_changed_value_adds_a_second_document(database):
    oura = FakeOuraService(personal_info={"weight": 78.0})
    service = build(database, oura)
    await service.sync_one(Resource.PERSONAL_INFO)

    oura._personal_info = {"weight": 79.5}
    result = await service.sync_one(Resource.PERSONAL_INFO)

    assert result == {"resource": "personal_info", "changed": True}
    assert len(database["resource_snapshots"].documents) == 2


async def test_a_date_ranged_resource_uses_the_last_record_of_the_day(database):
    """ring_configuration/ring_battery_level are date-ranged; a day can have
    more than one entry, and the most recent one is what gets stored."""
    oura = FakeOuraService(records=[{"battery_level": 80}, {"battery_level": 76}])
    service = build(database, oura)

    result = await service.sync_one(Resource.RING_BATTERY_LEVEL)

    assert result == {"resource": "ring_battery_level", "changed": True}
    assert database["resource_snapshots"].documents[0]["payload"] == {"battery_level": 76}


async def test_no_records_yet_today_is_reported_without_storing_anything(database):
    oura = FakeOuraService(records=[])
    service = build(database, oura)

    result = await service.sync_one(Resource.RING_CONFIGURATION)

    assert result["changed"] is False
    assert "reason" in result
    assert database["resource_snapshots"].documents == []
