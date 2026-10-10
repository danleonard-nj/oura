"""Ring wear status: whether the ring is on a finger, and since when.

Returned by the `oura_wear_status` / `oura_wear_intervals` tools and the
`/api/wear/*` routes. Kept terse for the same reason as `results.py`: field
descriptions become JSON Schema, and the prose belongs in tool descriptions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class WearStatus(StrEnum):
    WORN = "WORN"
    NOT_WORN = "NOT_WORN"
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WearState(BaseModel):
    """Current wear status, judged from the freshest evidence Oura has synced.

    `status` is UNKNOWN whenever the evidence is older than the freshness
    limit, whatever the last poll concluded -- `polled_status` keeps that.
    `since` is when the current WORN/NOT_WORN period began. `source` names the
    Oura collections the judgement rests on.
    """

    status: WearStatus
    confidence: Confidence
    reason: str
    source: list[str]
    since: datetime | None = None
    last_worn_at: datetime | None = None
    latest_evidence_at: datetime | None = None
    activity_through: datetime | None = None
    heart_rate_through: datetime | None = None
    evidence_age_minutes: float | None = None
    fresh: bool
    polled_status: WearStatus | None = None
    checked_at: datetime | None = None
    last_error: str | None = None


class WearInterval(BaseModel):
    """One period the ring was worn or not, from Oura's 5-minute activity classes."""

    status: WearStatus
    start: datetime
    end: datetime
    minutes: int


class WearTransition(BaseModel):
    """A live-detected change between WORN and NOT_WORN, at the evidence's time."""

    status: WearStatus
    previous: WearStatus | None = None
    at: datetime
    detected_at: datetime
    confidence: Confidence


class WearHistory(BaseModel):
    """Retrospective wear intervals and live transitions over a window, oldest first.

    `covered_through` is the end of the newest classified 5-minute slot.
    """

    start: datetime
    end: datetime
    covered_through: datetime | None = None
    worn_minutes: int
    not_worn_minutes: int
    intervals: list[WearInterval]
    transitions: list[WearTransition]
