"""The Oura v2 resources this server exposes, and how each behaves."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Resource(StrEnum):
    """Oura API v2 user collections.

    Values match Oura's own path segments so they read the same in logs, cache
    keys and audit records as they do in the upstream API.
    """

    DAILY_SLEEP = "daily_sleep"
    DAILY_READINESS = "daily_readiness"
    DAILY_ACTIVITY = "daily_activity"
    DAILY_STRESS = "daily_stress"
    DAILY_SPO2 = "daily_spo2"
    DAILY_RESILIENCE = "daily_resilience"
    DAILY_CARDIOVASCULAR_AGE = "daily_cardiovascular_age"
    VO2_MAX = "vO2_max"
    SLEEP = "sleep"
    SLEEP_TIME = "sleep_time"
    HEARTRATE = "heartrate"
    WORKOUT = "workout"
    SESSION = "session"
    TAG = "tag"
    ENHANCED_TAG = "enhanced_tag"
    REST_MODE_PERIOD = "rest_mode_period"
    RING_CONFIGURATION = "ring_configuration"
    RING_BATTERY_LEVEL = "ring_battery_level"
    PERSONAL_INFO = "personal_info"


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """Static facts about a resource that drive validation and caching."""

    resource: Resource
    #: False for singletons like personal_info that take no date range.
    date_ranged: bool = True
    #: Uses datetime rather than date bounds (heart rate is a time series).
    datetime_ranged: bool = False
    #: Overrides the global maximum span; None means use the configured default.
    max_days: int | None = None
    #: Data that effectively never changes gets the long "static" TTL.
    static: bool = False
    #: The OAuth scope Oura actually requires. Verified by probing every
    #: endpoint with a live token -- three of these are absent from Oura's
    #: OpenAPI spec, and Oura answers 401 rather than 403 when one is missing.
    scope: str = "daily"


SPECS: dict[Resource, ResourceSpec] = {
    Resource.DAILY_SLEEP: ResourceSpec(Resource.DAILY_SLEEP),
    Resource.DAILY_READINESS: ResourceSpec(Resource.DAILY_READINESS),
    Resource.DAILY_ACTIVITY: ResourceSpec(Resource.DAILY_ACTIVITY),
    Resource.DAILY_STRESS: ResourceSpec(Resource.DAILY_STRESS),
    Resource.DAILY_SPO2: ResourceSpec(Resource.DAILY_SPO2, scope="spo2"),
    Resource.DAILY_RESILIENCE: ResourceSpec(Resource.DAILY_RESILIENCE, scope="stress"),
    Resource.DAILY_CARDIOVASCULAR_AGE: ResourceSpec(Resource.DAILY_CARDIOVASCULAR_AGE, scope="heart_health"),
    Resource.VO2_MAX: ResourceSpec(Resource.VO2_MAX, scope="heart_health"),
    Resource.SLEEP: ResourceSpec(Resource.SLEEP),
    Resource.SLEEP_TIME: ResourceSpec(Resource.SLEEP_TIME),
    Resource.HEARTRATE: ResourceSpec(
        Resource.HEARTRATE, datetime_ranged=True, max_days=14, scope="heartrate"
    ),
    Resource.WORKOUT: ResourceSpec(Resource.WORKOUT, scope="workout"),
    Resource.SESSION: ResourceSpec(Resource.SESSION, scope="session"),
    Resource.TAG: ResourceSpec(Resource.TAG, scope="tag"),
    Resource.ENHANCED_TAG: ResourceSpec(Resource.ENHANCED_TAG, scope="tag"),
    Resource.REST_MODE_PERIOD: ResourceSpec(Resource.REST_MODE_PERIOD),
    Resource.RING_CONFIGURATION: ResourceSpec(
        Resource.RING_CONFIGURATION, static=True, scope="ring_configuration"
    ),
    Resource.RING_BATTERY_LEVEL: ResourceSpec(
        Resource.RING_BATTERY_LEVEL, scope="ring_configuration"
    ),
    Resource.PERSONAL_INFO: ResourceSpec(
        Resource.PERSONAL_INFO, date_ranged=False, static=True, scope="personal"
    ),
}


def spec_for(resource: Resource) -> ResourceSpec:
    return SPECS[resource]


#: Oura webhook `data_type` values mapped onto the resources they invalidate.
#: A webhook says "a sleep document changed"; both `sleep` and the derived
#: `daily_sleep` summary become stale.
#:
#: Keys are Oura's webhook `data_type` enum, which is NOT identical to the
#: collection path: the VO2 max collection is `vO2_max` but its webhook type is
#: `vo2_max`. `meal` is in the enum with no matching v2 collection, so it is
#: intentionally absent and falls through as unmapped.
WEBHOOK_DATA_TYPE_RESOURCES: dict[str, tuple[Resource, ...]] = {
    "tag": (Resource.TAG, Resource.ENHANCED_TAG),
    "enhanced_tag": (Resource.ENHANCED_TAG,),
    "workout": (Resource.WORKOUT,),
    "session": (Resource.SESSION,),
    "sleep": (Resource.SLEEP, Resource.DAILY_SLEEP),
    "daily_sleep": (Resource.DAILY_SLEEP,),
    "daily_readiness": (Resource.DAILY_READINESS,),
    "daily_activity": (Resource.DAILY_ACTIVITY,),
    "daily_spo2": (Resource.DAILY_SPO2,),
    "daily_stress": (Resource.DAILY_STRESS,),
    "daily_resilience": (Resource.DAILY_RESILIENCE,),
    "daily_cardiovascular_age": (Resource.DAILY_CARDIOVASCULAR_AGE,),
    "vo2_max": (Resource.VO2_MAX,),
    "sleep_time": (Resource.SLEEP_TIME,),
    "rest_mode_period": (Resource.REST_MODE_PERIOD,),
    "ring_configuration": (Resource.RING_CONFIGURATION,),
}
