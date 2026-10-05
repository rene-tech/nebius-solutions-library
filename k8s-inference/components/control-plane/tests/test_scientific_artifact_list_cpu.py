"""Large artifact inventories must not monopolize the shared API event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_scientific_batch_execution_handoff import artifact

from fs2_serve import scientific_artifacts as module
from fs2_serve.scientific_artifacts import ArtifactAccess, PostgresArtifactRepository


def rows_and_records(count):
    operation_id = uuid4()
    records = [
        artifact(operation_id, uuid4(), str(index).encode(), "application/octet-stream", ArtifactAccess())
        for index in range(count)
    ]
    rows = [
        {
            **record.model_dump(),
            "id": record.artifact_id,
            "shard_id": record.shard_id or module.NO_SHARD,
            "access_profile": record.access.profile,
            "access_receipt_digest": record.access.receipt_digest,
        }
        for record in records
    ]
    return operation_id, rows, records


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test20k_artifact_projection_keeps_heartbeat_scope_and_cancellation(monkeypatch, cancel):
    operation_id, rows, expected = rows_and_records(20007)
    attempt_id = uuid4()
    loop_thread = threading.get_ident()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    projection_threads = set()
    decoded = 0
    original = module._artifact_from_row

    async def fetch(query, *parameters):
        assert "operation_id=$1 AND tenant_id=$2" in query
        assert "ORDER BY stage_id,shard_id,attempt_id,id" in query
        assert parameters == (operation_id, "academic-poc", "input", attempt_id)
        return rows

    def gated_decode(row):
        nonlocal decoded
        projection_threads.add(threading.get_ident())
        if decoded == 0:
            started.set()
            if not release.wait(3):
                raise RuntimeError("artifact list decoding blocked the API event loop")
        result = original(row)
        decoded += 1
        if decoded == len(rows):
            finished.set()
        return result

    monkeypatch.setattr(module, "_artifact_from_row", gated_decode)
    repository = PostgresArtifactRepository(SimpleNamespace(fetch=fetch))
    task = asyncio.create_task(
        repository.list_artifacts(operation_id, tenant_id="academic-poc", stage_id="input", attempt_id=attempt_id)
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=4)
        assert projection_threads and loop_thread not in projection_threads
        # No DB work is moved into the thread; independent I/O/health probes
        # remain schedulable while the fully validating projection is busy.
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
    finally:
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == expected
    assert finished.is_set() and decoded == 20007


@pytest.mark.asyncio
async def test_off_loop_inventory_still_rejects_invalid_scoped_metadata():
    operation_id, rows, _ = rows_and_records(2)
    rows[-1]["storage_key"] = rows[0]["storage_key"]

    async def fetch(*_):
        return rows

    repository = PostgresArtifactRepository(SimpleNamespace(fetch=fetch))
    with pytest.raises(ValueError, match="storage key"):
        await repository.list_artifacts(operation_id, tenant_id="academic-poc")
