from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from test_scientific_admin import OPERATION_ID, RunAdapter, _context, _runtime, _service
from test_scientific_admin_postgres import BatchRepository, FakeConnection, FakePool, ModelAdapter, _record, _state

from fs2_serve.admin import AdminProblemError
from fs2_serve.api import create_app
from fs2_serve.models import TerminalAccounting
from fs2_serve.reporting_reads import InFlightMetricsRead, read_phase, reporting_connection, scientific_read_trace
from fs2_serve.scientific_admin_postgres import PostgresScientificRunAdminAdapter


async def test_overlapping_reads_share_bytes_but_next_read_is_fresh():
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return f"observation-{calls}".encode()

    reader = InFlightMetricsRead(collect)
    tasks = [asyncio.create_task(reader.read()) for _ in range(8)]
    await entered.wait()
    assert calls == 1
    gate.set()
    assert await asyncio.gather(*tasks) == [b"observation-1"] * 8
    assert await reader.read() == b"observation-2"
    assert calls == 2
    await reader.close()


async def test_failure_is_shared_without_old_result_or_poisoned_next_read():
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        if calls == 1:
            return b"old"
        if calls == 2:
            entered.set()
            await gate.wait()
            raise RuntimeError("database unavailable")
        return b"fresh"

    reader = InFlightMetricsRead(collect)
    assert await reader.read() == b"old"
    tasks = [asyncio.create_task(reader.read()) for _ in range(3)]
    await entered.wait()
    gate.set()
    failures = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(error, RuntimeError) for error in failures)
    assert calls == 2
    assert await reader.read() == b"fresh"
    await reader.close()


async def test_one_cancelled_waiter_does_not_cancel_others():
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def collect():
        entered.set()
        await gate.wait()
        return b"shared"

    reader = InFlightMetricsRead(collect)
    first = asyncio.create_task(reader.read())
    second = asyncio.create_task(reader.read())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    gate.set()
    assert await second == b"shared"
    await reader.close()


async def test_all_cancelled_waiters_release_work_and_next_request_recovers():
    entered = asyncio.Event()
    released = asyncio.Event()
    calls = 0

    async def collect():
        nonlocal calls
        calls += 1
        if calls > 1:
            return b"new"
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            released.set()

    reader = InFlightMetricsRead(collect)
    waiter = asyncio.create_task(reader.read())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(released.wait(), 1)
    assert await reader.read() == b"new"
    await reader.close()


async def test_shutdown_cancels_and_drains_inflight_work():
    entered = asyncio.Event()

    async def collect():
        entered.set()
        await asyncio.Event().wait()
        return b"unreachable"

    reader = InFlightMetricsRead(collect)
    waiter = asyncio.create_task(reader.read())
    await entered.wait()
    await reader.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not reader._tasks


def traces(caplog):
    return [json.loads(row.message) for row in caplog.records if row.name == "fs2_serve.reporting_reads"]


async def test_real_adapter_logs_phase_and_pool_timings_without_history_payloads(caplog):
    caplog.set_level(logging.INFO)
    state = _state()
    connection = FakeConnection(_record(state))
    adapter = PostgresScientificRunAdminAdapter(
        pool=FakePool(connection), batches=BatchRepository(), models=ModelAdapter()
    )
    request_id = uuid4()
    with scientific_read_trace(OPERATION_ID, request_id):
        detail = await adapter.get_run(OPERATION_ID, tenant_id=None)
    trace = traces(caplog)[-1]
    assert trace["request_id"] == str(request_id)
    assert trace["operation_id"] == str(OPERATION_ID)
    assert trace["error_type"] is None
    names = {phase["phase"] for phase in trace["phases"]}
    assert {
        "run_record.pool_wait",
        "run_record.queries",
        "run_record.pool_release",
        "decode_state",
        "model_inventory_and_catalog",
        "events_repository",
        "detail_projection",
    } <= names
    assert all(phase["elapsed_ms"] >= 0 for phase in trace["phases"])
    assert detail.data.run.id == str(OPERATION_ID)
    assert "svc-cd8-design" not in json.dumps(trace)
    assert "SELECT" not in json.dumps(trace)


async def test_timeout_records_cancelled_pool_phase_and_real_timeout_without_exception_detail(caplog):
    caplog.set_level(logging.INFO)

    class BlockedPool:
        @asynccontextmanager
        async def acquire(self):
            await asyncio.Event().wait()
            yield None

    class BlockedRuns(RunAdapter):
        async def get_run(self, operation_id, *, tenant_id):
            async with reporting_connection(BlockedPool(), "run_record"):
                raise AssertionError("unreachable")

    service = _service(runs=BlockedRuns())
    service.adapter_timeout_seconds = 0.01  # Test duration only; production default stays two seconds.
    request_id = uuid4()
    with pytest.raises(AdminProblemError) as error:
        await service.run_detail(_context(), OPERATION_ID, tenant_id=None, request_id=request_id)
    assert error.value.status_code == 503
    trace = traces(caplog)[-1]
    assert trace["error_type"] == "TimeoutError"
    assert trace["phases"][0]["phase"] == "run_record.pool_wait"
    assert trace["phases"][0]["error_type"] == "CancelledError"
    assert trace["request_id"] == str(request_id)


async def test_trace_state_isolated_across_concurrent_requests_and_reset(caplog):
    caplog.set_level(logging.INFO)
    identifiers = [uuid4(), uuid4()]

    async def read(request_id):
        with scientific_read_trace(OPERATION_ID, request_id), read_phase("catalog"):
            await asyncio.sleep(0)

    await asyncio.gather(*(read(identity) for identity in identifiers))
    with read_phase("outside"):
        pass
    assert {row["request_id"] for row in traces(caplog)} == {str(value) for value in identifiers}
    assert all([p["phase"] for p in row["phases"]] == ["catalog"] for row in traces(caplog))


async def test_overlapping_http_scrapes_keep_queues_live_while_bounded_history_refreshes(
    registry,
    cipher,
    hasher,
    monkeypatch,
):
    runtime = _runtime(registry, cipher, hasher)
    slots = asyncio.Semaphore(2)
    entered = asyncio.Event()
    gate = asyncio.Event()
    calls = 0
    rows = [
        TerminalAccounting(
            model_id="qwen3-8b",
            protocol="test",
            outcome="succeeded",
            operations=12,
            estimated_gpu_seconds=24,
            duration_seconds=48,
            cold_start_seconds=3,
        )
    ]

    async def terminal():
        nonlocal calls
        calls += 1
        async with slots:
            entered.set()
            await gate.wait()
            return rows

    class SharedPoolHistory(RunAdapter):
        async def get_run(self, operation_id, *, tenant_id):
            async with slots:
                return await super().get_run(operation_id, tenant_id=tenant_id)

    monkeypatch.setattr(runtime.store, "terminal_accounting", terminal)
    runtime.scientific_admin = _service(runs=SharedPoolHistory())
    assert runtime.scientific_admin.adapter_timeout_seconds == 2
    app = create_app(runtime)
    clock = [100.0]
    app.state.historical_metrics._monotonic = lambda: clock[0]
    queued = [3]

    async def queue_counts():
        return {("qwen3-8b", "queued"): queued[0]}

    async def queue_age():
        return {"qwen3-8b": float(queued[0])}

    monkeypatch.setattr(runtime.store, "queue_counts", queue_counts)
    monkeypatch.setattr(runtime.store, "oldest_queue_age", queue_age)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://inference.test.invalid"
    ) as client:
        assert (
            await client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + "a" * 32})
        ).status_code == 200
        scrapes = [asyncio.create_task(client.get("/metrics")) for _ in range(6)]
        await entered.wait()
        detail = await asyncio.wait_for(client.get(f"/admin/api/v1/scientific-runs/{OPERATION_ID}"), 1)
        assert detail.status_code == 200
        assert UUID(detail.headers["x-request-id"])
        assert calls == 1
        responses = await asyncio.wait_for(asyncio.gather(*scrapes), 1)
        assert all(response.status_code == 200 for response in responses)
        assert len({response.content for response in responses}) == 1
        assert "fs2_serve_historical_accounting_available 0.0" in responses[0].text
        assert "fs2_serve_requests_total{" not in responses[0].text
        queued[0] = 4
        live = await asyncio.wait_for(client.get("/metrics"), 1)
        assert 'fs2_serve_operations{model="qwen3-8b",state="queued"} 4.0' in live.text
        assert 'fs2_serve_oldest_queued_operation_age_seconds{model="qwen3-8b"} 4.0' in live.text
        assert calls == 1
        gate.set()
        await asyncio.wait_for(app.state.historical_metrics._task, 1)
        sampled = await client.get("/metrics")
        assert 'fs2_serve_requests_total{model="qwen3-8b",outcome="succeeded",protocol="test"} 12.0' in sampled.text
        rows[0] = rows[0].model_copy(update={"operations": 13})
        retained = await client.get("/metrics")
        assert calls == 1
        assert 'fs2_serve_requests_total{model="qwen3-8b",outcome="succeeded",protocol="test"} 12.0' in retained.text
        clock[0] += 30
        expired = await client.get("/metrics")
        assert "fs2_serve_requests_total{" not in expired.text
        await asyncio.wait_for(app.state.historical_metrics._task, 1)
        fresh = await client.get("/metrics")
        assert calls == 2
        assert 'fs2_serve_requests_total{model="qwen3-8b",outcome="succeeded",protocol="test"} 13.0' in fresh.text
    await app.state.historical_metrics.close()


async def test_admin_error_response_and_log_share_server_generated_request_id(registry, cipher, hasher, caplog):
    caplog.set_level(logging.INFO)

    class Broken(RunAdapter):
        async def get_run(self, operation_id, *, tenant_id):
            with read_phase("events_repository"):
                raise ValueError("PRIVATE SQL and customer payload must never be logged")

    runtime = _runtime(registry, cipher, hasher)
    runtime.scientific_admin = _service(runs=Broken())
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://inference.test.invalid"
    ) as client:
        await client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + "a" * 32})
        response = await client.get(
            f"/admin/api/v1/scientific-runs/{OPERATION_ID}", headers={"x-request-id": "untrusted"}
        )
    assert response.status_code == 503
    request_id = response.headers["x-request-id"]
    assert UUID(request_id)
    assert response.json()["request_id"] == request_id
    trace = traces(caplog)[-1]
    assert trace["request_id"] == request_id and trace["error_type"] == "ValueError"
    assert "PRIVATE SQL" not in caplog.text and "PRIVATE SQL" not in response.text


async def test_late_historical_failure_omits_old_accounting_but_keeps_live_metrics(
    registry, cipher, hasher, monkeypatch
):
    runtime = _runtime(registry, cipher, hasher)
    count = 12
    fail = False

    async def terminal():
        return [
            TerminalAccounting(
                model_id="qwen3-8b",
                protocol="test",
                outcome="succeeded",
                operations=count,
                estimated_gpu_seconds=24,
                duration_seconds=48,
                cold_start_seconds=3,
            )
        ]

    async def rollups():
        if fail:
            raise RuntimeError("late accounting unavailable")
        return []

    monkeypatch.setattr(runtime.store, "terminal_accounting", terminal)
    monkeypatch.setattr(runtime.lifecycle, "rollup_metric_rows", rollups)
    app = create_app(runtime)
    clock = [100.0]
    app.state.historical_metrics._monotonic = lambda: clock[0]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://inference.test.invalid") as client:
        first = await client.get("/metrics")
        assert first.status_code == 200
        assert "fs2_serve_historical_accounting_available 0.0" in first.text
        await asyncio.wait_for(app.state.historical_metrics._task, 1)
        good = await client.get("/metrics")
        metric = 'fs2_serve_requests_total{model="qwen3-8b",outcome="succeeded",protocol="test"}'
        assert metric + " 12.0" in good.text
        count = 13
        fail = True
        clock[0] += 30
        failed = await client.get("/metrics")
        await asyncio.wait_for(app.state.historical_metrics._task, 1)
        assert failed.status_code == 200  # Queue metrics survive unavailable historical accounting.
        assert metric not in failed.text  # Neither stale12 nor partial13 is published.
        assert "fs2_serve_historical_accounting_available 0.0" in failed.text
        assert "fs2_serve_historical_accounting_age_seconds 30.0" in failed.text
        fail = False
        clock[0] += 30
        await client.get("/metrics")
        await asyncio.wait_for(app.state.historical_metrics._task, 1)
        fresh = await client.get("/metrics")
        assert fresh.status_code == 200
        assert metric + " 13.0" in fresh.text
    await app.state.historical_metrics.close()


async def test_live_queue_failure_still_fails_scrape_instead_of_using_old_queue(registry, cipher, hasher, monkeypatch):
    runtime = _runtime(registry, cipher, hasher)

    async def unavailable():
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(runtime.store, "queue_counts", unavailable)
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="https://inference.test.invalid"
    ) as client:
        assert (await client.get("/metrics")).status_code == 503
        assert app.state.historical_metrics._task is None
    await app.state.historical_metrics.close()
