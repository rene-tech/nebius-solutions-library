"""In-flight-only scrape sharing and payload-free scientific read diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

LOGGER = logging.getLogger(__name__)


@dataclass
class _Scrape:
    task: asyncio.Task[bytes]
    waiters: int = 0


class InFlightMetricsRead:
    """Share only overlapping reads, never serve a completed/stale cached result.

    One cancelled HTTP waiter cannot cancel the other scrapes. If all waiters
    leave, cancel the underlying work to release its database connection.
    """

    def __init__(self, collect: Callable[[], Awaitable[bytes]]) -> None:
        self.collect = collect
        self._scrape: _Scrape | None = None
        self._tasks: set[asyncio.Task[bytes]] = set()

    def _finished(self, task: asyncio.Task[bytes]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve an error even if the last HTTP caller disconnected.

    async def read(self) -> bytes:
        scrape = self._scrape
        if scrape is None or scrape.task.done() or scrape.task.cancelling():
            task = asyncio.create_task(self.collect(), name="fs2-metrics-read")
            self._tasks.add(task)
            task.add_done_callback(self._finished)
            scrape = self._scrape = _Scrape(task)
        scrape.waiters += 1
        try:
            return await asyncio.shield(scrape.task)
        finally:
            scrape.waiters -= 1
            if scrape.waiters == 0 and not scrape.task.done():
                scrape.task.cancel()

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class _ReadTrace:
    request_id: UUID | None
    operation_id: UUID
    phases: list[dict[str, Any]]


_TRACE: ContextVar[_ReadTrace | None] = ContextVar("fs2_scientific_read_trace", default=None)


@contextmanager
def scientific_read_trace(operation_id: UUID, request_id: UUID | None) -> Iterator[None]:
    """One summary log, including cancellation phase, without payloads or SQL."""
    trace = _ReadTrace(request_id, operation_id, [])
    token = _TRACE.set(trace)
    started = time.monotonic()
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        _TRACE.reset(token)
        LOGGER.log(
            logging.WARNING if error_type else logging.INFO,
            "%s",
            json.dumps(
                {
                    "event": "scientific_history_read",
                    "request_id": str(trace.request_id) if trace.request_id else None,
                    "operation_id": str(trace.operation_id),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "error_type": error_type,
                    "phases": trace.phases,
                },
                separators=(",", ":"),
            ),
        )


@contextmanager
def read_phase(name: str) -> Iterator[None]:
    trace = _TRACE.get()
    if trace is None:
        yield
        return
    started = time.monotonic()
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        trace.phases.append(
            {"phase": name, "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "error_type": error_type}
        )


class _DiagnosticAcquire:
    def __init__(self, context: Any, name: str) -> None:
        self.context = context
        self.name = name

    async def __aenter__(self) -> Any:
        with read_phase(self.name + ".pool_wait"):
            return await self.context.__aenter__()

    async def __aexit__(self, *args: Any) -> Any:
        with read_phase(self.name + ".pool_release"):
            return await self.context.__aexit__(*args)


@asynccontextmanager
async def reporting_connection(pool: Any, name: str) -> AsyncIterator[Any]:
    async with _DiagnosticAcquire(pool.acquire(), name) as connection:
        with read_phase(name + ".queries"):
            yield connection
