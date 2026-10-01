"""Historical snapshots of Oura resources that have no history behind them.

Oura's live API only ever answers "what is it right now" for things like ring
configuration/firmware or the profile; `ring_battery_level` accepts a date
range but Oura does not durably retain a long history behind it either. This
is the shape that gives them one: one document per *change*, not per poll.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class ResourceSnapshot(BaseModel):
    """One row in `resource_snapshots`: a resource's value as of a point in time.

    `content_hash` is what makes this a change log rather than a poll log --
    a new document is only inserted when the payload actually differs from
    the most recent one for the same `(account_id, resource)`; an identical
    poll just bumps `last_seen_at` on the existing latest document.
    """

    account_id: str
    resource: str
    content_hash: str
    payload: dict[str, Any] | list[Any]
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
