"""Error types shared across the Oura integration layer.

Every upstream failure is translated into one of these before it leaves the
service layer, so route handlers and MCP tools never see `requests` exceptions
and never risk echoing an Authorization header back to a caller.
"""

from __future__ import annotations


class OuraError(Exception):
    """Base class for anything that went wrong talking to Oura.

    `message` is safe to show a caller: it never contains tokens, headers or
    client credentials.
    """

    status_code: int | None = None

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code

    @property
    def category(self) -> str:
        """Short, stable label for audit records and metrics."""
        return type(self).__name__.removesuffix("Error").lower() or "oura"


class OuraValidationError(OuraError):
    """Oura rejected the request parameters (HTTP 400), or we caught it first."""

    status_code = 400


class OuraAuthError(OuraError):
    """The access token was missing, invalid or expired (HTTP 401)."""

    status_code = 401


class OuraPermissionError(OuraError):
    """Scope not granted, or the user's Oura subscription does not cover it (403)."""

    status_code = 403


class OuraNotFoundError(OuraError):
    """No such document (HTTP 404)."""

    status_code = 404


class OuraRateLimitError(OuraError):
    """Rate limit exceeded (HTTP 429)."""

    status_code = 429

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OuraUpstreamError(OuraError):
    """Oura returned 5xx, or the request timed out / failed at the network layer."""

    status_code = 502


class OuraNotConnectedError(OuraError):
    """No OAuth token is stored for this account yet."""

    status_code = 428


class TokenRefreshError(OuraError):
    """The refresh attempt itself failed."""

    status_code = 502


class TokenRefreshUnavailableError(TokenRefreshError):
    """Refresh could not be attempted safely.

    Raised when Redis is unreachable. Oura refresh tokens are single-use, so
    refreshing without the distributed lock risks two replicas burning the same
    token and locking the account out. Failing is the safe outcome.
    """

    status_code = 503


class DateRangeError(OuraValidationError):
    """The requested date range is malformed or too expensive to serve."""
