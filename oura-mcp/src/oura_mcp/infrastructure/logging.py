"""Structured stdout logging with a correlation id carried across the request."""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

# Substrings that mark a value as credential-bearing. Matching is on the *key*,
# so a field named `access_token` is redacted wherever it appears in the tree.
_SENSITIVE_KEYS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "client_secret",
    "code",
    "signature",
    "encryption_key",
)

# Belt-and-braces for credentials interpolated into a message string.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{8,}")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b([a-z_]*(?:token|secret|password|api_?key)[a-z_]*)\s*[=:]\s*['\"]?([A-Za-z0-9\-._~+/=]{8,})"
)

REDACTED = "***redacted***"

STANDARD_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


def redact_text(text: str) -> str:
    text = _BEARER.sub(r"\1 " + REDACTED, text)
    return _ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


def is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and any(s in key.lower() for s in _SENSITIVE_KEYS)


def redact_field(key: object, value: Any) -> Any:
    """Redact by field name, then by content. Used for top-level `extra=` fields."""
    return REDACTED if is_sensitive_key(key) else redact(value)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively strip credential-shaped values out of a structure."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(key) else redact(item, _depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class StructuredFormatter(logging.Formatter):
    """Emits one readable line, with any ``extra=`` fields appended."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        correlation_id = getattr(record, "correlation_id", None) or get_correlation_id()
        line = f"{timestamp} {record.levelname:<8} {record.name} [{correlation_id}] {redact_text(record.getMessage())}"

        fields = []
        for key, value in record.__dict__.items():
            if key not in STANDARD_RECORD_FIELDS and key != "correlation_id":
                fields.append(f"{key}={_format_value(redact_field(key, value))}")
        if fields:
            line += " | " + " ".join(fields)
        if record.exc_info:
            line += " | exception=" + _format_value(redact_text(self.formatException(record.exc_info)))
        return line


def _format_value(value: Any) -> str:
    """Keep fields compact while making strings and collections unambiguous."""
    if isinstance(value, str):
        return repr(value)
    return repr(value)


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = get_correlation_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Install the structured handler on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(CorrelationIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty and duplicate what we already log ourselves: httpx2
    # logs one INFO line per outbound call ("HTTP Request: GET ..."), which is
    # the same request the audit service logs as `oura_request` with the
    # status, timing and resource already attached. urllib3/requests/pymongo
    # are transitive dependencies with the same problem, and their payloads
    # can contain URLs with query strings.
    for noisy in ("httpx2", "urllib3", "requests", "pymongo"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
