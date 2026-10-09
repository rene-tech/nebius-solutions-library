"""Scientific admission must preserve atomicity without blocking API heartbeats."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import AdmissionRequest, Principal, Scope, TokenCreate
from fs2_serve.postgres import PostgresStore
from fs2_serve.store import ConflictError


@pytest_asyncio.fixture(params=("memory", "postgres"))
async def admission_store(request, cipher, hasher):
    if request.param == "memory":
        yield MemoryStore(cipher, hasher)
        return
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(
        database_url, Path(__file__).parents[1] / "migrations", cipher, hasher, payload_ttl_seconds=3600,
    )
    await store.migrate()
    async with store.pool.acquire() as connection:
        await connection.execute("TRUNCATE fs2_operations,fs2_tokens RESTART IDENTITY CASCADE")
    try:
        yield store
    finally:
        async with store.pool.acquire() as connection:
            await connection.execute("TRUNCATE fs2_operations,fs2_tokens RESTART IDENTITY CASCADE")
        await store.close()


async def arguments(store):
    token_id = uuid4()
    identity = Principal(
        token_id=token_id, token_prefix=f"fs2_pat_{token_id.hex[:12]}", tenant_id="test-tenant",
        principal_id="scientist", scopes=frozenset({Scope.INFERENCE_INVOKE.value}),
        models=frozenset({"gromacs"}), max_concurrency=2,
    )
    await store.issue_token(
        token_id=token_id, prefix=identity.token_prefix, pepper_key_id="pepper-v1", digest="test-digest",
        request=TokenCreate(
            principal_id=identity.principal_id, tenant_id=identity.tenant_id,
            scopes={Scope.INFERENCE_INVOKE}, models={"gromacs"}, max_concurrency=2,
        ), created_by="test-owner",
    )
    return dict(
        principal=identity,
        admission=AdmissionRequest(
            model_id="gromacs", operation="workflow", protocol="scientific-batch-v1",
            idempotency_key="scientific-cpu-admission-0001", request_body=b'{"input":"original"}',
        ), model_revision="test", reserved_gpu_seconds=0, max_attempts=1,
    )


@pytest.mark.asyncio
async def test_heartbeat_and_concurrent_replay_keep_one_atomic_freeze(admission_store):
    store = admission_store
    request = await arguments(store)
    started, release = threading.Event(), threading.Event()
    factory_threads = []
    loop_thread = threading.get_ident()

    def freeze(operation):
        factory_threads.append(threading.get_ident())
        started.set()
        if not release.wait(3):
            raise RuntimeError("CPU factory blocked the API event loop")
        return {"operation_id": str(operation.id), "immutable": "original"}

    first = asyncio.create_task(store.append_operation(**request, scientific_admission_factory=freeze))
    replay = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=4)
        # This coroutine (and therefore health checks) can execute while the
        # factory remains busy. Direct synchronous execution fails this bound.
        assert len(factory_threads) == 1 and factory_threads[0] != loop_thread
        assert not first.done()
        replay = asyncio.create_task(store.append_operation(**request, scientific_admission_factory=freeze))
        await asyncio.sleep(0)
        assert not replay.done()
    finally:
        release.set()
        operations = await asyncio.gather(first, *([replay] if replay is not None else []))
    assert len(operations) == 2
    assert operations[0].id == operations[1].id and operations[1].reused
    assert len(factory_threads) == 1
    token = await store.get_token(request["principal"].token_id)
    assert token.requests_used == 1
    assert len(await store.list_scientific_admissions()) == 1
    original = await store.get_scientific_admission(operations[0].id)
    assert original is not None and original.payload["immutable"] == "original"
    with pytest.raises(ConflictError, match="different request"):
        await store.append_operation(
            **{**request, "admission": request["admission"].model_copy(update={"request_body": b"different"})},
            scientific_admission_factory=freeze,
        )
    assert len(factory_threads) == 1


@pytest.mark.asyncio
async def test_factory_error_rolls_back_outbox_and_usage(admission_store):
    store = admission_store
    request = await arguments(store)

    def invalid(_):
        raise ValueError("invalid scientific plan")

    with pytest.raises(ValueError, match="invalid scientific plan"):
        await store.append_operation(**request, scientific_admission_factory=invalid)
    assert (await store.get_token(request["principal"].token_id)).requests_used == 0
    assert await store.list_scientific_admissions() == []
    successful = await store.append_operation(**request, scientific_admission_factory=lambda _: {"valid": True})
    assert not successful.reused
    assert (await store.get_token(request["principal"].token_id)).requests_used == 1


@pytest.mark.asyncio
async def test_cancelled_factory_finishes_before_releasing_atomic_scope(admission_store):
    store = admission_store
    request = await arguments(store)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def freeze(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("test did not release CPU worker")
        finished.set()
        return {"valid": True}

    task = asyncio.create_task(store.append_operation(**request, scientific_admission_factory=freeze))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=4)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert (await store.get_token(request["principal"].token_id)).requests_used == 0
    assert await store.list_scientific_admissions() == []
    successful = await store.append_operation(**request, scientific_admission_factory=lambda _: {"valid": True})
    assert not successful.reused
