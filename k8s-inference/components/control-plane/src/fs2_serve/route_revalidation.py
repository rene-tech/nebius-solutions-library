"""Periodic and dispatch-time canonical route evidence revalidation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from .registry import Registry

LOGGER = logging.getLogger(__name__)


class RouteRevalidator:
    """Serialize full canonical reloads while readers use immutable snapshots."""

    def __init__(
        self,
        registry: Registry,
        *,
        interval_seconds: float,
    ) -> None:
        if not 1 <= interval_seconds <= 300:
            raise ValueError("route revalidation interval is outside the closed bound")
        self.registry = registry
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def refresh(self, *, validation_time: datetime | None = None) -> bool:
        """Reload the trust root and every signed route subject before dispatch."""

        async with self._lock:
            valid = await asyncio.to_thread(self.registry.revalidate, validation_time=validation_time)
        if not valid:
            # Catalog exceptions can contain filesystem or provider details.
            # The registry exposes only bounded health state; logs stay generic.
            LOGGER.warning("canonical route evidence revalidation failed; routes withdrawn")
        return valid

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self.refresh()
        self._task = asyncio.create_task(self._run(), name="fs2-route-revalidation")

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    await self.refresh()
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def health(self) -> dict[str, object]:
        state = self.registry.validation_health()
        state["periodic_task_healthy"] = self._task is not None and not self._task.done()
        return state
