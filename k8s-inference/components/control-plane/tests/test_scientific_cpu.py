"""CPU isolation must not weaken locks, cancellation or process-wide bounds."""

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from fs2_serve.scientific_cpu import run_scientific_cpu


async def wait_thread_event(event):
    deadline = time.monotonic() + 5
    while not event.is_set():
        assert time.monotonic() < deadline, "worker did not reach the expected boundary"
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_result_exception_thread_identity_and_context_are_preserved():
    marker = contextvars.ContextVar("scientific-test-marker", default="outside")
    marker.set("request-context")
    owner = threading.get_ident()
    value, worker, context = await run_scientific_cpu(
        lambda number: (number + 1, threading.get_ident(), marker.get()), 6
    )
    assert (value, context) == (7, "request-context") and worker != owner

    def fail():
        raise ValueError("pure computation failed")

    with pytest.raises(ValueError, match="pure computation failed"):
        await run_scientific_cpu(fail)


def test_two_active_workers_are_bounded_across_multiple_event_loops():
    lock = threading.Lock()
    active = peak = 0

    def compute():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return threading.current_thread().name

    def caller():
        return asyncio.run(run_scientific_cpu(compute))

    with ThreadPoolExecutor(max_workers=6) as clients:
        names = list(clients.map(lambda _: caller(), range(12)))
    assert active == 0 and peak == 2
    assert all(name.startswith("fs2-scientific-cpu") for name in names)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_error", [False, True])
async def test_raw_repeated_task_cancellation_keeps_owner_lock_until_work_finishes(callback_error):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    lock = asyncio.Lock()

    def compute():
        entered.set()
        assert release.wait(5)
        finished.set()
        if callback_error:
            raise ValueError("finished after the caller was cancelled")
        return "finished"

    async def owner():
        async with lock:
            return await run_scientific_cpu(compute)

    task = asyncio.create_task(owner())
    try:
        await wait_thread_event(entered)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert lock.locked() and not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_queued_work_never_starts_and_does_not_consume_worker_capacity():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    unwanted = threading.Event()

    def hold(index):
        entered[index].set()
        assert release.wait(5)

    running = [asyncio.create_task(run_scientific_cpu(hold, index)) for index in range(2)]
    try:
        for event in entered:
            await wait_thread_event(event)
        queued = asyncio.create_task(run_scientific_cpu(unwanted.set))
        await asyncio.sleep(0.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not unwanted.is_set()
    finally:
        release.set()
        await asyncio.gather(*running)
    assert await run_scientific_cpu(lambda: 42) == 42
    assert not unwanted.is_set()
