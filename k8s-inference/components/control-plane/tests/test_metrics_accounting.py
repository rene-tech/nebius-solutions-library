from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from prometheus_client.parser import text_string_to_metric_families

from fs2_serve.metrics_accounting import HistoricalMetricsRead, HistoricalSample, historical_reporting_connection
from fs2_serve.models import TerminalAccounting
from fs2_serve.telemetry import HistoricalAccounting, Metrics


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


async def finish(reader):
    assert reader._task is not None
    await asyncio.wait_for(asyncio.shield(reader._task), 1)


async def test_history_never_blocks_read_and_only_one_refresh_runs_per_interval():
    clock = Clock()
    gate = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        await gate.wait()
        return (calls,)

    reader = HistoricalMetricsRead(collect, monotonic=clock, wall_time=lambda: 1000 + clock())
    assert reader.read() == HistoricalSample(None, None, None, True)
    for _ in range(100):
        assert reader.read().value is None
    await asyncio.sleep(0)
    assert calls == 1
    gate.set()
    await finish(reader)
    assert reader.read() == HistoricalSample((1,), 0, 1100, False)
    clock.now += 29
    assert reader.read().value == (1,)
    assert calls == 1
    clock.now += 1
    assert reader.read() == HistoricalSample(None, 30, 1100, True)
    await finish(reader)
    assert reader.read() == HistoricalSample((2,), 0, 1130, False)
    await reader.close()


async def test_collection_age_includes_the_queries_not_just_completion():
    clock = Clock()

    async def collect():
        clock.now += 2
        return "sample"

    reader = HistoricalMetricsRead(collect, monotonic=clock, wall_time=lambda: 1000 + clock())
    reader.read()
    await finish(reader)
    assert reader.read() == HistoricalSample("sample", 2, 1100, False)
    clock.now += 28
    assert reader.read().value is None
    await reader.close()


async def test_failure_omits_expired_data_and_does_not_retry_each_scrape(caplog):
    clock = Clock()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("SECRET SQL, credential and request content")
        return "observed"

    reader = HistoricalMetricsRead(collect, monotonic=clock)
    reader.read()
    await finish(reader)
    clock.now += 30
    assert reader.read().value is None
    await finish(reader)
    for _ in range(6):
        assert reader.read().value is None
        assert not reader.read().refreshing
        clock.now += 4
    assert calls == 2
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text
    await reader.close()


async def test_timeout_cancels_work_and_backs_off_without_duplicate_query():
    clock = Clock()
    released = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    reader = HistoricalMetricsRead(collect, monotonic=clock, timeout_seconds=0.01)
    reader.read()
    await finish(reader)
    assert released.is_set()
    assert reader.read().value is None and not reader.read().refreshing
    assert calls == 1
    clock.now += 30
    reader.read()
    await finish(reader)
    assert calls == 2
    await reader.close()


async def test_cancelled_scrape_does_not_cancel_or_duplicate_bounded_history():
    clock = Clock()
    entered = asyncio.Event()
    gate = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return "complete"

    reader = HistoricalMetricsRead(collect, monotonic=clock)

    async def scrape():
        reader.read()
        await asyncio.Event().wait()

    cancelled = asyncio.create_task(scrape())
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert reader.read().refreshing
    assert calls == 1
    gate.set()
    await finish(reader)
    assert reader.read().value == "complete"
    await reader.close()


async def test_shutdown_drains_refresh_and_cannot_restart_it():
    entered = asyncio.Event()
    released = asyncio.Event()

    async def collect():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            released.set()

    reader = HistoricalMetricsRead(collect)
    reader.read()
    await entered.wait()
    await reader.close()
    assert released.is_set()
    assert reader.read().value is None and not reader.read().refreshing


@pytest.mark.parametrize("timeout,max_age", [(0, 30), (4, 30), (3, 2)])
def test_invalid_refresh_budget_is_rejected(timeout, max_age):
    async def collect():
        return None

    with pytest.raises(ValueError):
        HistoricalMetricsRead(collect, timeout_seconds=timeout, max_age_seconds=max_age)


def samples(rendered):
    return {
        sample.name: sample.value
        for family in text_string_to_metric_families(rendered.decode())
        for sample in family.samples
    }


def test_unknown_or_expired_history_omits_families_not_live_queue_or_zeroes():
    metrics = Metrics([])
    rows = HistoricalAccounting(
        terminal=(
            TerminalAccounting(
                model_id="gromacs",
                protocol="test",
                outcome="succeeded",
                operations=12,
                estimated_gpu_seconds=24,
                duration_seconds=48,
                cold_start_seconds=3,
            ),
        ),
        semantics=(),
        operations=None,
        lifecycle=(),
        rollups=(),
    )
    metrics.set_historical_accounting(rows)
    metrics.set_queue({("gromacs", "queued"): 3})
    metrics.set_queue_age({"gromacs": 12})
    first = samples(metrics.render(historical=HistoricalSample(None, None, None, True)))
    assert first["fs2_serve_historical_accounting_available"] == 0
    assert "fs2_serve_historical_accounting_age_seconds" not in first
    assert "fs2_serve_requests_total" not in first
    assert first["fs2_serve_oldest_queued_operation_age_seconds"] == 12
    fresh = samples(metrics.render(historical=HistoricalSample(rows, 2, 1000, False)))
    assert fresh["fs2_serve_requests_total"] == 12
    assert fresh["fs2_serve_historical_accounting_available"] == 1
    assert fresh["fs2_serve_historical_accounting_age_seconds"] == 2
    stale = samples(metrics.render(historical=HistoricalSample(None, 32, 1000, True)))
    assert stale["fs2_serve_historical_accounting_available"] == 0
    assert stale["fs2_serve_historical_accounting_age_seconds"] == 32
    assert "fs2_serve_requests_total" not in stale
    assert stale["fs2_serve_oldest_queued_operation_age_seconds"] == 12


async def test_read_only_transaction_settings_are_local_and_rolled_back_on_failure():
    events = []

    class Connection:
        @asynccontextmanager
        async def transaction(self, **kwargs):
            events.append(("begin", kwargs))
            try:
                yield
            finally:
                events.append("transaction_finished")

        async def execute(self, sql):
            events.append(sql)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            try:
                yield Connection()
            finally:
                events.append("pool_released")

    with pytest.raises(RuntimeError):
        async with historical_reporting_connection(Pool()):
            raise RuntimeError("unavailable")
    assert events == [
        ("begin", {"readonly": True}),
        "SET LOCAL max_parallel_workers_per_gather = 0",
        "SET LOCAL statement_timeout = '3s'",
        "transaction_finished",
        "pool_released",
    ]
