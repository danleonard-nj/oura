"""The Oura integration layer.

Everything above this line -- MCP tools, HTTP routes -- deals in resources,
date ranges and plain dictionaries. Everything below it deals in bearer tokens,
`requests` exceptions and worker threads. This service is the seam.

It owns three things:

* the cache read/write around each fetch,
* translating upstream failures into `OuraError` subclasses, and
* the retry policy, including the refresh-and-retry-once path for a 401.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx2

from oura_mcp.clients.oura_client import OuraApiClient
from oura_mcp.config import Config
from oura_mcp.infrastructure.logging import get_correlation_id, get_logger
from oura_mcp.models.audit import AuditAction, AuditEvent, CacheStatus
from oura_mcp.models.dates import DateRange, resolve_range
from oura_mcp.models.errors import (
    OuraAuthError,
    OuraError,
    OuraNotFoundError,
    OuraPermissionError,
    OuraRateLimitError,
    OuraUpstreamError,
    OuraValidationError,
)
from oura_mcp.models.resources import Resource, spec_for
from oura_mcp.services.audit_service import AuditService
from oura_mcp.services.cache_service import CacheService
from oura_mcp.services.token_service import TokenService

logger = get_logger(__name__)

#: Statuses worth trying again. 401 is handled separately: it triggers a token
#: refresh and exactly one retry, not backoff.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0


class OuraFetch:
    """A fetch result plus how it was served."""

    __slots__ = ("records", "cache")

    def __init__(self, records: Any, cache: CacheStatus) -> None:
        self.records = records
        self.cache = cache


class OuraService:
    def __init__(
        self,
        client: OuraApiClient,
        token_service: TokenService,
        cache: CacheService,
        audit: AuditService,
        config: Config,
    ) -> None:
        self._client = client
        self._tokens = token_service
        self._cache = cache
        self._audit = audit
        self._account_id = config.oura_account_id
        self._max_attempts = config.oura_max_attempts
        self._max_range_days = config.oura_max_range_days
        self._max_heartrate_days = config.oura_max_heartrate_days

    @property
    def account_id(self) -> str:
        return self._account_id

    # ── Range resolution ──────────────────────────────────────────────────

    def resolve_range(
        self,
        resource: Resource,
        start_date: str | None,
        end_date: str | None,
        *,
        default_days: int = 7,
    ) -> DateRange:
        """Validate and bound a requested range for one resource.

        Heart rate is sampled continuously, so it carries a much tighter
        ceiling than the daily summaries.
        """
        spec = spec_for(resource)
        max_days = spec.max_days or self._max_range_days
        if resource is Resource.HEARTRATE:
            max_days = min(max_days, self._max_heartrate_days)
        return resolve_range(
            start_date, end_date, default_days=min(default_days, max_days), max_days=max_days
        )

    # ── Fetching ──────────────────────────────────────────────────────────

    async def get_records(
        self,
        resource: Resource,
        date_range: DateRange,
        *,
        use_cache: bool = True,
    ) -> OuraFetch:
        """Fetch every document for a resource over a date range."""
        params = date_range.as_params()

        if use_cache:
            cached, status = await self._cache.get(self._account_id, resource, params)
            if status is CacheStatus.HIT:
                return OuraFetch(cached, status)
        else:
            status = CacheStatus.BYPASS

        records = await self._call(
            resource,
            lambda token: self._client.fetch_range(
                token, resource, date_range.start.isoformat(), date_range.end.isoformat()
            ),
            date_range=date_range,
        )

        if use_cache:
            await self._cache.set(
                self._account_id, resource, params, records, date_range=date_range
            )
        return OuraFetch(records, status)

    async def get_personal_info(self, *, use_cache: bool = True) -> OuraFetch:
        resource = Resource.PERSONAL_INFO
        params: dict[str, Any] = {}

        if use_cache:
            cached, status = await self._cache.get(self._account_id, resource, params)
            if status is CacheStatus.HIT:
                return OuraFetch(cached, status)
        else:
            status = CacheStatus.BYPASS

        document = await self._call(resource, self._client.fetch_personal_info)

        if use_cache:
            await self._cache.set(self._account_id, resource, params, document)
        return OuraFetch(document, status)

    # ── Execution, retry and translation ──────────────────────────────────

    async def _call(self, resource: Resource, operation, *, date_range: DateRange | None = None):
        """Run one blocking Oura operation with retries and error translation.

        `operation` takes an access token and returns the awaitable that
        performs the call.
        """
        started = time.perf_counter()
        refreshed = False
        attempt = 0

        if (denied := await self._scope_denial(resource)) is not None:
            await self._audit_request(
                resource, date_range, started, status="error", error=denied
            )
            raise denied

        while True:
            attempt += 1
            token = await self._tokens.get_access_token(self._account_id)
            try:
                result = await operation(token)
            except Exception as exc:  # noqa: BLE001 - translated immediately below
                error = self._translate(exc, resource)

                if isinstance(error, OuraAuthError) and not refreshed:
                    # The stored token was rejected. Refresh once under the
                    # distributed lock and give the operation a single retry.
                    logger.info(
                        "oura rejected the access token; refreshing",
                        extra={
                            "resource": resource.value, "attempt": attempt,
                            "upstream_status": 401,
                        },
                    )
                    refreshed = True
                    try:
                        await self._tokens.refresh(self._account_id, rejected_access_token=token)
                    except OuraError as refresh_error:
                        await self._audit_request(
                            resource, date_range, started, status="error", error=refresh_error
                        )
                        raise
                    continue

                if self._should_retry(error, attempt):
                    delay = self._backoff(error, attempt)
                    logger.warning(
                        "retrying oura request",
                        extra={
                            "resource": resource.value,
                            "attempt": attempt,
                            "delay_seconds": round(delay, 2),
                            "upstream_status": error.status_code,
                            "error_category": error.category,
                        },
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.warning(
                    "oura request failed after recovery attempts",
                    extra={
                        "resource": resource.value,
                        "attempt": attempt,
                        "refresh_attempted": refreshed,
                        "upstream_status": error.upstream_status,
                        "error_category": error.category,
                    },
                )
                await self._audit_request(
                    resource, date_range, started, status="error", error=error
                )
                raise error from exc

            await self._audit_request(resource, date_range, started, status="ok")
            return result

    async def _scope_denial(self, resource: Resource) -> OuraPermissionError | None:
        """Refuse locally when the granted scopes cannot cover this resource.

        Oura answers 401 for a missing scope, so without this every call to an
        unreachable collection costs a round trip and looks like an auth
        failure. `oura_summary` fans out over several collections, so one
        ungranted scope would otherwise tax every summary.

        Unknown scopes mean no opinion: if the stored token records none, the
        request goes out and Oura decides.
        """
        granted = await self._tokens.granted_scopes(self._account_id)
        if not granted:
            return None

        required = spec_for(resource).scope
        if required in granted:
            return None

        return OuraPermissionError(
            f"Access to {resource.value} needs the '{required}' Oura scope, which this "
            f"connection was not granted. Reconnect the account with '{required}' in "
            "OURA_SCOPES. Not requested upstream, because Oura would reject it."
        )

    def _should_retry(self, error: OuraError, attempt: int) -> bool:
        if attempt >= self._max_attempts:
            return False
        return error.status_code in RETRYABLE_STATUSES or isinstance(error, OuraUpstreamError)

    def _backoff(self, error: OuraError, attempt: int) -> float:
        """Exponential backoff with jitter, deferring to Retry-After when Oura sends one."""
        if isinstance(error, OuraRateLimitError) and error.retry_after is not None:
            return min(error.retry_after, BACKOFF_CAP_SECONDS * 2)
        delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
        return delay * (0.5 + random.random() / 2)

    def _translate(self, exc: Exception, resource: Resource) -> OuraError:
        """Turn a client-layer exception into a safe, actionable OuraError.

        Nothing derived from the request headers reaches the message, so a
        bearer token can never be echoed back to a caller.
        """
        if isinstance(exc, OuraError):
            return exc

        if isinstance(exc, httpx2.HTTPStatusError):
            error = self._translate_status(exc.response, resource)
            error.upstream_status = exc.response.status_code
            return error

        if isinstance(exc, httpx2.TimeoutException):
            return OuraUpstreamError(f"The Oura API timed out fetching {resource.value}.")

        if isinstance(exc, httpx2.TransportError):
            return OuraUpstreamError(f"Could not reach the Oura API to fetch {resource.value}.")

        if isinstance(exc, ValueError):
            return OuraValidationError(str(exc))

        logger.error(
            "unexpected failure calling oura",
            exc_info=exc,
            extra={"resource": resource.value},
        )
        return OuraUpstreamError(f"Unexpected failure fetching {resource.value} from Oura.")

    def _translate_status(self, response: httpx2.Response, resource: Resource) -> OuraError:
        status = response.status_code
        spec = spec_for(resource)

        if status == 400:
            return OuraValidationError(
                f"Oura rejected the parameters for {resource.value}. "
                "Check that the date range is valid and not in the future."
            )
        if status == 401:
            # Oura answers 401 for a missing *scope*, not just an expired
            # token, and says which one in the body:
            #   {"detail": "Token is not authorized access stress scope."}
            # Treating that as expiry would refresh and retry pointlessly, and
            # tell the caller nothing useful, so split on the message.
            if missing := _missing_scope(response):
                return OuraPermissionError(
                    f"Access to {resource.value} needs the '{missing}' Oura scope, which "
                    "this connection was not granted. Add it to OURA_SCOPES and reconnect "
                    "the account. Note that Oura does not list this scope in its OpenAPI "
                    "spec, and returns 401 rather than 403 for it."
                )
            return OuraAuthError(f"The Oura access token was rejected fetching {resource.value}.")
        if status == 403:
            return OuraPermissionError(
                f"Access to {resource.value} was refused. This usually means the "
                f"'{spec.scope}' scope was not granted when the account was connected, "
                "or the data needs an active Oura subscription."
            )
        if status == 404:
            return OuraNotFoundError(f"Oura has no {resource.value} record matching that request.")
        if status == 429:
            return OuraRateLimitError(
                f"Oura rate limit reached fetching {resource.value}. "
                "Retry shortly, or narrow the date range.",
                retry_after=_retry_after(response),
            )
        if status >= 500:
            return OuraUpstreamError(
                f"The Oura API returned {status} fetching {resource.value}.", status_code=502
            )
        return OuraUpstreamError(
            f"The Oura API returned an unexpected {status} fetching {resource.value}.",
            status_code=502,
        )

    async def _audit_request(
        self,
        resource: Resource,
        date_range: DateRange | None,
        started: float,
        *,
        status: str,
        error: OuraError | None = None,
    ) -> None:
        await self._audit.record(
            AuditEvent(
                action=AuditAction.OURA_REQUEST,
                account_id=self._account_id,
                correlation_id=get_correlation_id(),
                status=status,
                resource=resource.value,
                start_date=date_range.start.isoformat() if date_range else None,
                end_date=date_range.end.isoformat() if date_range else None,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                upstream_status=error.upstream_status if error else 200,
                error_category=error.category if error else None,
                error_message=error.message if error else None,
            )
        )


def _missing_scope(response: httpx2.Response) -> str | None:
    """Pull the scope name out of Oura's 401 body, if that is what it is.

    Undocumented behaviour, so it is matched defensively: anything unexpected
    just falls through and the 401 is treated as an expired token.
    """
    try:
        detail = str(response.json().get("detail", ""))
    except (ValueError, AttributeError):
        return None

    if "not authorized" not in detail or "scope" not in detail:
        return None

    words = detail.replace("scope.", "").replace("scope", "").split()
    return words[-1] if words else None


def _retry_after(response: httpx2.Response) -> float | None:
    """Read Oura's Retry-After header, ignoring anything unparseable."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
