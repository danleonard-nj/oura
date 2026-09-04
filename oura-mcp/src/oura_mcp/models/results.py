"""Structured shapes returned by MCP tools.

Tools return these models so the SDK emits a real output schema and clients get
typed objects rather than stringified JSON.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


# Docstrings and field descriptions on these models become JSON Schema, and
# RecordSet's schema is emitted once per record-returning tool -- fourteen
# copies. They are kept terse for that reason; the prose belongs in the tool
# descriptions, which are emitted once each.
class RecordSet(BaseModel):
    """Oura documents for one resource over an inclusive date range.

    `cache` is hit, miss, bypass or disabled. `records` are Oura documents
    verbatim, timestamps ISO-8601.
    """

    resource: str
    start_date: date | None = None
    end_date: date | None = None
    count: int
    cache: str
    records: list[dict[str, Any]]


class Document(BaseModel):
    """A single Oura document, for singleton resources like personal_info."""

    resource: str
    cache: str
    document: dict[str, Any]


class DaySummary(BaseModel):
    """Everything known about one calendar day, keyed by Oura's `day` field."""

    day: date
    sleep: dict[str, Any] | None = Field(default=None, description="Daily sleep summary and score.")
    readiness: dict[str, Any] | None = Field(default=None, description="Daily readiness summary and score.")
    activity: dict[str, Any] | None = Field(default=None, description="Daily activity summary and score.")
    stress: dict[str, Any] | None = Field(default=None, description="Daily stress and recovery summary.")
    resilience: dict[str, Any] | None = Field(default=None, description="Daily resilience level, if available.")
    spo2: dict[str, Any] | None = Field(default=None, description="Daily average blood oxygen, if available.")
    workouts: list[dict[str, Any]] = Field(default_factory=list, description="Workouts recorded that day.")


class Summary(BaseModel):
    """Composite multi-resource view over a date range."""

    start_date: date
    end_date: date
    days: list[DaySummary] = Field(description="One entry per day with any data, oldest first.")
    unavailable: list[str] = Field(
        default_factory=list,
        description=(
            "Resources that could not be included, each as 'resource: reason'. A resource may "
            "be missing because the account lacks the scope or an Oura subscription."
        ),
    )
