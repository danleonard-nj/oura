"""Bounded thread pool for the blocking Oura client.

`oura-ring` is built on synchronous `requests`. Calling it from a coroutine
would block the event loop for the whole round trip, stalling every other MCP
session on the replica. Everything that touches it goes through here instead.

A bounded pool rather than `asyncio.to_thread` is deliberate: the default
executor is unbounded, and Oura enforces per-token and per-application rate
limits, so a hard ceiling on concurrent upstream requests is the useful
behaviour. This is the only place that knows the client is synchronous, so
swapping in a native async client later means deleting this indirection rather
than rewriting callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

from oura_mcp.config import Config

P = ParamSpec("P")
T = TypeVar("T")


class ThreadOffloader:
    """Runs blocking callables on a bounded worker pool."""

    def __init__(self, config: Config) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=config.oura_executor_workers,
            thread_name_prefix="oura-io",
        )

    async def run(self, func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        loop = asyncio.get_running_loop()
        if kwargs:
            return await loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))
        return await loop.run_in_executor(self._executor, func, *args)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
