"""Small response and query-string helpers shared by the route blueprints."""

from __future__ import annotations

from typing import Any

from quart import Response, jsonify, request

from app.utilities.logging import get_correlation_id


def json_response(body: Any, status: int = 200) -> Response:
    response = jsonify(body)
    response.status_code = status
    return response


def json_error(message: str, status: int, *, error: str = "error") -> Response:
    return json_response(
        {"error": error, "detail": message, "correlation_id": get_correlation_id()},
        status,
    )


def int_arg(name: str, *, default: int, maximum: int) -> int:
    """A positive integer query parameter, clamped to `maximum`."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError:
        return default
