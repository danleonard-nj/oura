"""SummaryService: fan-out and partial-availability behaviour.

`OuraService` itself (retry, cache, translation) is covered in
oura-common/tests/test_oura_service.py; this is the one summary-specific test
that needs a real `OuraService` wired to a stub client and lives here because
`SummaryService` is MCP-specific, not part of oura-common.
"""

from __future__ import annotations

from datetime import date

from oura_common.models.dates import DateRange
from oura_common.services.audit_service import AuditService
from oura_common.services.cache_service import CacheService
from oura_common.services.oura_service import OuraService
from tests.conftest import FakeAuditRepository, FakeRedis

from oura_common.models.resources import Resource

from oura_mcp.services.summary_service import SummaryService

RANGE = DateRange(start=date(2026, 9, 1), end=date(2026, 9, 3))


class RecordingClient:
    def __init__(self, result=None) -> None:
        self.result = result if result is not None else [{"day": "2026-09-01", "score": 80}]
        self.calls: list[tuple] = []

    async def fetch_range(self, token, resource, start, end):
        self.calls.append((resource, start, end))
        return self.result


class StubTokenService:
    def __init__(self, scopes) -> None:
        self.scopes = set(scopes)

    async def granted_scopes(self, account_id: str) -> set[str]:
        return self.scopes

    async def get_access_token(self, account_id: str) -> str:
        return "token-a"

    async def refresh(self, account_id: str, *, seen_version=None, rejected_access_token=None):
        return None


def build_service(config, client, tokens):
    redis = FakeRedis()
    audit = AuditService(FakeAuditRepository())  # type: ignore[arg-type]
    return OuraService(
        client=client,  # type: ignore[arg-type]
        token_service=tokens,  # type: ignore[arg-type]
        cache=CacheService(redis, config),  # type: ignore[arg-type]
        audit=audit,
        config=config,
    )


async def test_a_summary_skips_only_the_ungranted_resource(config):
    """One missing scope must not cost a round trip on every summary call."""
    client = RecordingClient()
    tokens = StubTokenService(
        scopes={"daily", "spo2", "workout", "personal", "heartrate", "tag", "session"}
    )
    service = build_service(config, client, tokens)

    summary = await SummaryService(service).build(RANGE)

    requested = {call[0] for call in client.calls}
    assert Resource.DAILY_RESILIENCE not in requested
    assert Resource.DAILY_SLEEP in requested
    assert any("daily_resilience" in note for note in summary.unavailable)
