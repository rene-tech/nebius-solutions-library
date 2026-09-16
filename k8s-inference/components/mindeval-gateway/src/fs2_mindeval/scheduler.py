"""One process, round-robin teams, five active calls/team, sliding RPM/TPM.

Deployment deliberately uses one replica/worker. Every provider attempt (including
retries) goes through this scheduler. Tokens are reserved conservatively using
UTF-8 byte count and settled to provider usage, never silently oversubscribed.
"""

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .contracts import GatewayError


@dataclass
class Job:
    team: str
    model: str
    tokens: int
    limit: int
    future: asyncio.Future
    call: Callable[[], Awaitable]
    created: float


class FairScheduler:
    def __init__(
        self, *, window: float = 60, global_concurrency: int = 50, default_rpm: int = 600, default_tpm: int = 400_000
    ):
        self.window, self.global_concurrency = window, global_concurrency
        self.default_limits = (default_rpm, default_tpm)
        self.limits = {}
        self.queues = defaultdict(deque)
        self.rotation = deque()
        self.active = defaultdict(int)
        self.ledger = defaultdict(deque)
        self.running = set()
        self.wake = asyncio.Event()
        self.dispatcher = None
        self.closed = False

    def configure(self, models: list[dict]):
        for model in models:
            limits = model.get("per_request_limits", {})
            self.limits[model["id"]] = (
                int(limits.get("requests_per_minute") or self.default_limits[0]),
                int(limits.get("tokens_per_minute") or self.default_limits[1]),
            )

    async def submit(self, *, team: str, model: str, tokens: int, limit: int, call: Callable[[], Awaitable]):
        if self.closed:
            raise GatewayError("shutting_down", "gateway is restarting; resume the operation", status=503)
        if tokens > self.limits.get(model, self.default_limits)[1]:
            raise GatewayError("request_too_large", "request exceeds model TPM reservation budget", status=422)
        loop = asyncio.get_running_loop()
        job = Job(team, model, tokens, min(5, limit), loop.create_future(), call, time.monotonic())
        if team not in self.rotation:
            self.rotation.append(team)
        self.queues[team].append(job)
        if self.dispatcher is None:
            self.dispatcher = asyncio.create_task(self._dispatch())
        self.wake.set()
        try:
            return await job.future
        finally:
            self.wake.set()

    def _admissible(self, job: Job, now: float):
        ledger = self.ledger[job.model]
        while ledger and ledger[0][0] <= now - self.window:
            ledger.popleft()
        rpm, tpm = self.limits.get(job.model, self.default_limits)
        return (
            self.active[job.team] < job.limit
            and len(ledger) < rpm
            and sum(entry[1] for entry in ledger) + job.tokens <= tpm
        )

    async def _dispatch(self):
        while not self.closed:
            self.wake.clear()
            progressed = False
            for _ in range(len(self.rotation)):
                team = self.rotation.popleft()
                queue = self.queues[team]
                queue = deque(job for job in queue if not job.future.cancelled())
                self.queues[team] = queue
                if not queue:
                    continue
                self.rotation.append(team)
                if sum(self.active.values()) >= self.global_concurrency:
                    break
                # A blocked model cannot prevent another model from serving the team.
                for job in queue:
                    if self._admissible(job, time.monotonic()):
                        queue.remove(job)
                        self.active[team] += 1
                        entry = [time.monotonic(), job.tokens]
                        self.ledger[job.model].append(entry)
                        task = asyncio.create_task(self._execute(job, entry))
                        self.running.add(task)
                        task.add_done_callback(self.running.discard)
                        progressed = True
                        break
            if progressed:
                await asyncio.sleep(0)
            else:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=min(0.1, self.window))
                except TimeoutError:
                    pass

    async def _execute(self, job: Job, entry: list):
        queue_ms = (time.monotonic() - job.created) * 1000
        try:
            result = await job.call()
            # Only settle from actual provider usage; failed attempts retain reservation.
            if isinstance(result, dict) and isinstance(result.get("usage"), dict):
                actual = result["usage"].get("total_tokens")
                if isinstance(actual, int) and actual >= 0:
                    entry[1] = actual
            if not job.future.done():
                job.future.set_result((result, queue_ms))
        except Exception as exc:
            if not job.future.done():
                job.future.set_exception(exc)
        finally:
            self.active[job.team] -= 1
            self.wake.set()

    def status(self):
        return {
            "active": sum(self.active.values()),
            "queued": sum(map(len, self.queues.values())),
            "teams_active": sum(value > 0 for value in self.active.values()),
            "workers_per_team": 5,
        }

    async def close(self):
        self.closed = True
        self.wake.set()
        if self.dispatcher:
            await self.dispatcher
        for queue in self.queues.values():
            for job in queue:
                if not job.future.done():
                    job.future.set_exception(GatewayError("shutting_down", "gateway restarted", status=503))
        if self.running:
            await asyncio.gather(*self.running, return_exceptions=True)
