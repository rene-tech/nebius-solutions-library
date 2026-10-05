"""Large outbox recovery has one fleet owner and never decodes on the API loop."""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import test_scientific_admission_cpu as admission_tests
import test_scientific_batch_postgres_state as postgres_tests
from test_scientific_batch_production import scientific_runtime

from fs2_serve import postgres as postgres_module
from fs2_serve.models import PendingScientificAdmission
from fs2_serve.postgres import PostgresStore
from fs2_serve.scientific_cpu import run_scientific_cpu

admission_store = admission_tests.admission_store
postgres_store = postgres_tests.store


def peer(store):
    """A second replica's owner state using a different pooled connection."""
    return PostgresStore(store.pool, store.migrations_dir, store.cipher, store.hasher, store.payload_ttl_seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "list"])
async def test_postgres_outbox_decode_is_off_loop(postgres_store, monkeypatch, method):
    request = await admission_tests.arguments(postgres_store)
    operation = await postgres_store.append_operation(
        **request, scientific_admission_factory=lambda _: {"immutable": "original"},
    )
    original = postgres_module._decode_configuration_json
    started, release = threading.Event(), threading.Event()
    decoder_threads = []
    loop_thread = threading.get_ident()

    def decode(value, label):
        decoder_threads.append(threading.get_ident())
        started.set()
        if not release.wait(5):
            raise RuntimeError("outbox JSON decoding blocked the API loop")
        return original(value, label)

    monkeypatch.setattr(postgres_module, "_decode_configuration_json", decode)
    task = asyncio.create_task(
        postgres_store.get_scientific_admission(operation.id)
        if method == "get" else postgres_store.list_scientific_admissions()
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
        assert len(decoder_threads) == 1
        assert decoder_threads[0] != loop_thread
        assert not task.done()
    finally:
        release.set()
        result = await task
    items = [result] if method == "get" else result
    assert len(items) == 1 and items[0].operation_id == operation.id
    assert items[0].payload == {"immutable": "original"}


@pytest.mark.asyncio
async def test_recovery_owner_is_nonblocking_and_direct_admission_is_independent(admission_store):
    store = admission_store
    other = peer(store) if isinstance(store, PostgresStore) else store
    async with store.scientific_admission_recovery() as acquired:
        assert acquired
        async with store.scientific_admission_recovery() as local_contender:
            assert not local_contender
        async with other.scientific_admission_recovery() as remote_contender:
            assert not remote_contender
        request = await admission_tests.arguments(store)
        operation = await asyncio.wait_for(
            store.append_operation(**request, scientific_admission_factory=lambda _: {"valid": True}), timeout=5,
        )
        assert not operation.reused
        assert (await store.get_scientific_admission(operation.id)).payload == {"valid": True}
    async with other.scientific_admission_recovery() as reacquired:
        assert reacquired


@pytest.mark.asyncio
async def test_recovery_error_releases_owner(admission_store):
    with pytest.raises(ValueError, match="test failure"):
        async with admission_store.scientific_admission_recovery() as acquired:
            assert acquired
            raise ValueError("test failure")
    async with admission_store.scientific_admission_recovery() as reacquired:
        assert reacquired


@pytest.mark.asyncio
async def test_raw_repeated_cancel_drains_computation_before_releasing_owner(admission_store):
    store = admission_store
    other = peer(store) if isinstance(store, PostgresStore) else store
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def compute():
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release computation")
        finished.set()

    async def owner():
        async with store.scientific_admission_recovery() as acquired:
            assert acquired
            await run_scientific_cpu(compute)

    task = asyncio.create_task(owner())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        async with other.scientific_admission_recovery() as contender:
            assert not contender
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    async with other.scientific_admission_recovery() as reacquired:
        assert reacquired


@pytest.mark.asyncio
async def test_postgres_repeated_cancel_drains_pool_reset(postgres_store, monkeypatch):
    store = postgres_store
    reset_started, reset_release = asyncio.Event(), asyncio.Event()
    original_release = type(store.pool).release
    intercepted = False

    async def gated_release(pool, connection, **kwargs):
        nonlocal intercepted
        if pool is store.pool and not intercepted:
            intercepted = True
            reset_started.set()
            await reset_release.wait()
        return await original_release(pool, connection, **kwargs)

    monkeypatch.setattr(type(store.pool), "release", gated_release)

    async def owner():
        async with store.scientific_admission_recovery() as acquired:
            assert acquired

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(reset_started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        async with peer(store).scientific_admission_recovery() as contender:
            assert not contender
    finally:
        reset_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with peer(store).scientific_admission_recovery() as reacquired:
        assert reacquired


@pytest.mark.asyncio
async def test_disconnected_postgres_owner_is_automatically_released(postgres_store):
    store = postgres_store
    unsigned = postgres_module._SCIENTIFIC_ADMISSION_RECOVERY_LOCK & ((1 << 64) - 1)
    async with store.scientific_admission_recovery() as acquired:
        assert acquired
        async with store.pool.acquire() as observer:
            # This is the task's disposable test database only. Resolve the
            # exact advisory holder rather than terminating arbitrary backends.
            owners = await observer.fetch(
                """SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted
                   AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                   AND classid=$1::oid AND objid=$2::oid AND objsubid=1""",
                unsigned >> 32, unsigned & ((1 << 32) - 1),
            )
            assert len(owners) == 1
            disconnected = asyncio.Event()
            owner_connection = next(
                holder._con for holder in store.pool._holders
                if holder._con is not None and holder._con.get_server_pid() == owners[0]["pid"]
            )
            owner_connection.add_termination_listener(lambda _: disconnected.set())
            assert await observer.fetchval("SELECT pg_terminate_backend($1)", owners[0]["pid"])
            await asyncio.wait_for(disconnected.wait(), timeout=5)
        async with peer(store).scientific_admission_recovery() as reacquired:
            assert reacquired
    async with store.scientific_admission_recovery() as recovered_local_owner:
        assert recovered_local_owner


@pytest.mark.asyncio
async def test_nine_background_workers_read_and_materialize_one_page(
    admission_store, registry, cipher, hasher, monkeypatch,
):
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    service = runtime.scientific_batches
    service.store = admission_store
    entered, release = asyncio.Event(), asyncio.Event()
    calls = {"list": 0, "materialize": 0}
    pending = PendingScientificAdmission(operation_id=uuid4(), payload={}, created_at=datetime.now(UTC))

    async def list_pending(*, limit):
        assert limit == 100
        calls["list"] += 1
        return [pending]

    async def materialize(item):
        assert item is pending
        calls["materialize"] += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(service.store, "list_scientific_admissions", list_pending)
    monkeypatch.setattr(service, "_materialize_pending", materialize)
    competitors = []
    for _ in range(8):
        competitor = copy.copy(service)
        if isinstance(admission_store, PostgresStore):
            competitor.store = peer(admission_store)
            monkeypatch.setattr(competitor.store, "list_scientific_admissions", list_pending)
        competitors.append(competitor)
    owner = asyncio.create_task(service.recover_pending_admissions())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        losers = await asyncio.wait_for(
            asyncio.gather(*(competitor.recover_pending_admissions() for competitor in competitors)), timeout=5,
        )
        assert losers == [0] * 8
        assert calls == {"list": 1, "materialize": 1}
    finally:
        release.set()
        assert await owner == 1
