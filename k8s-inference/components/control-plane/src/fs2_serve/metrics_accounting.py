"""Bound historical reporting work without delaying live queue observations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True)
class HistoricalSample(Generic[_T]):
    value: _T | None
    age_seconds: float | None
    sampled_at: float | None
    refreshing: bool


class HistoricalMetricsRead(Generic[_T]):
    """At most one bounded refresh per process and interval, including failures.

    This is only for historical accounting, never admission/queue state. Reads
    do not wait for the refresh. The age starts before the first database query,
    so a slow collection cannot make its oldest observation appear newer.
    Expired or never-observed values are absent, not zero or stale success.
    """

    def __init__(
        self,
        collect: Callable[[], Awaitable[_T]],
        *,
        max_age_seconds: float = 30,
        timeout_seconds: float = 3,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < timeout_seconds <= 3 or max_age_seconds < timeout_seconds:
            raise ValueError("historical accounting requires a positive <=3s budget and a longer refresh interval")
        self.collect = collect
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._task: asyncio.Task[None] | None = None
        self._value: _T | None = None
        self._sample_started: float | None = None
        self._sampled_at: float | None = None
        self._next_refresh = float("-inf")
        self._closed = False

    def read(self) -> HistoricalSample[_T]:
        now = self._monotonic()
        if not self._closed and now >= self._next_refresh and (self._task is None or self._task.done()):
            self._next_refresh = now + self.max_age_seconds
            self._task = asyncio.create_task(self._refresh(now, self._wall_time()), name="fs2-historical-metrics-read")
        age = None if self._sample_started is None else max(0.0, now - self._sample_started)
        return HistoricalSample(
            value=self._value if age is not None and age < self.max_age_seconds else None,
            age_seconds=age,
            sampled_at=self._sampled_at,
            refreshing=self._task is not None and not self._task.done(),
        )

    async def _refresh(self, started: float, sampled_at: float) -> None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                value = await self.collect()
        except Exception as error:
            # The connection/SQL/payload exception text must not enter shared logs.
            LOGGER.warning("historical metrics refresh unavailable (%s)", type(error).__name__)
        else:
            self._value = value
            self._sample_started = started
            self._sampled_at = sampled_at

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


@asynccontextmanager
async def historical_reporting_connection(pool: Any) -> AsyncIterator[Any]:
    """Limit reporting fan-out only inside this read-only transaction.

    Admission and other users of the pool retain their original settings. The
    server bound complements the aggregate refresh deadline, including clients
    that call one historical method directly rather than through /metrics.
    """
    async with pool.acquire() as connection, connection.transaction(readonly=True):
        await connection.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        await connection.execute("SET LOCAL statement_timeout = '3s'")
        yield connection
