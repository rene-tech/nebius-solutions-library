"""Supervised lifecycle for the durable scientific-batch reconciler."""

from __future__ import annotations

import asyncio
import logging

from .controller import ScientificBatchController

LOGGER = logging.getLogger(__name__)


class ScientificBatchWorker:
    def __init__(
        self,
        controller: ScientificBatchController,
        *,
        workers: int = 1,
        poll_seconds: float = 0.25,
    ) -> None:
        if not 1 <= workers <= 32 or not 0.05 <= poll_seconds <= 60:
            raise ValueError("scientific worker concurrency or poll interval is outside the bound")
        self.controller = controller
        self.workers = workers
        self.poll_seconds = poll_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = asyncio.Event()
        self._failures: list[int] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._closing.clear()
        self._failures = [0] * self.workers
        self._tasks = [
            asyncio.create_task(self._run(index), name=f"scientific-batch-{index}") for index in range(self.workers)
        ]

    async def _run(self, index: int) -> None:
        while not self._closing.is_set():
            try:
                await self.controller.reconcile_once()
                self._failures[index] = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures[index] += 1
                LOGGER.exception("scientific batch reconcile failed", extra={"worker": index})
            try:
                await asyncio.wait_for(self._closing.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closing.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict[str, object]:
        failures = max(self._failures, default=0)
        return {
            "ready": bool(self._tasks) and all(not task.done() for task in self._tasks) and failures < 3,
            "workers": len(self._tasks),
            "consecutive_failures": failures,
        }
