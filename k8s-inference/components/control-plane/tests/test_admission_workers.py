from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fs2_serve.admission import AdmissionService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import (
    AdmissionRequest,
    OperationStatus,
    Principal,
    RuntimeIdentity,
    RuntimeResult,
    Scope,
    TokenCreate,
)
from fs2_serve.runtime import PreemptedError, StubRuntimeClient
from fs2_serve.telemetry import Metrics


async def setup_principal(store: MemoryStore, *, concurrency: int = 4) -> Principal:
    token_id = uuid4()
    await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id="pepper-v1",
        digest="digest",
        request=TokenCreate(
            principal_id="worker-user",
            tenant_id="worker-tenant",
            scopes={Scope.INFERENCE_INVOKE},
            models={"qwen3-8b"},
            gpu_seconds_budget=1000,
            max_concurrency=concurrency,
        ),
        created_by="bootstrap-admin",
    )
    return Principal(
        token_id=token_id,
        token_prefix=f"fs2_pat_{token_id.hex[:12]}",
        principal_id="worker-user",
        tenant_id="worker-tenant",
        scopes=frozenset({"inference.invoke"}),
        models=frozenset({"qwen3-8b"}),
        gpu_seconds_budget=1000,
        max_concurrency=concurrency,
    )


def request(key: str, *, deadline_at: datetime | None = None) -> AdmissionRequest:
    return AdmissionRequest(
        model_id="qwen3-8b",
        operation="chat",
        protocol="openai-chat",
        idempotency_key=key,
        request_body=b'{"model":"qwen3-8b","messages":[{"role":"user","content":"private"}]}',
        deadline_at=deadline_at,
    )


def service(
    registry,
    store,
    runtime,
    *,
    lease: float = 0.3,
    grace: float = 1,
    max_waiters: int = 32,
    wait_initial: float = 0.05,
    wait_max: float = 0.5,
) -> AdmissionService:
    return AdmissionService(
        registry=registry,
        store=store,
        runtime=runtime,
        metrics=Metrics(registry.list(enabled_only=True)),
        worker_concurrency=1,
        poll_seconds=0.01,
        lease_seconds=lease,
        maintenance_interval_seconds=0.02,
        shutdown_grace_seconds=grace,
        max_sync_waiters=max_waiters,
        wait_poll_initial_seconds=wait_initial,
        wait_poll_max_seconds=wait_max,
    )


async def wait_status(store: MemoryStore, operation_id, wanted: set[OperationStatus], timeout: float = 2):
    async with asyncio.timeout(timeout):
        while True:
            current = await store.get_operation(operation_id)
            if current.status in wanted:
                return current
            await asyncio.sleep(0.01)


class CountingReadStore(MemoryStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.operation_reads = 0

    async def get_operation(self, operation_id, *, tenant_id=None):
        self.operation_reads += 1
        return await super().get_operation(operation_id, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_sync_wait_uses_bounded_exponential_polling(registry, cipher, hasher) -> None:
    store = CountingReadStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(
        registry,
        store,
        StubRuntimeClient(),
        wait_initial=0.05,
        wait_max=0.2,
    )
    operation = await admission.admit(principal, request("bounded-wait-poll-key-0001"))

    current = await admission.wait(operation.id, tenant_id=principal.tenant_id, seconds=0.36)

    assert current.status == OperationStatus.QUEUED
    assert 4 <= store.operation_reads <= 5
    assert admission.health()["sync_waiters"] == 0


@pytest.mark.asyncio
async def test_sync_wait_capacity_degrades_to_durable_operation_without_stranding(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store, concurrency=32)
    admission = service(registry, store, StubRuntimeClient(), max_waiters=4)
    operations = [await admission.admit(principal, request(f"wait-capacity-key-{index:04d}")) for index in range(1, 33)]
    held = [
        asyncio.create_task(admission.wait(item.id, tenant_id=principal.tenant_id, seconds=10))
        for item in operations[:4]
    ]
    try:
        async with asyncio.timeout(1):
            while admission.health()["sync_waiters"] != 4:
                await asyncio.sleep(0.01)
        started = asyncio.get_running_loop().time()
        overflow = await asyncio.gather(
            *(admission.wait(item.id, tenant_id=principal.tenant_id, seconds=10) for item in operations[4:])
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert [item.id for item in overflow] == [item.id for item in operations[4:]]
        assert all(item.status == OperationStatus.QUEUED for item in overflow)
        assert elapsed < 0.5
        rendered = admission.metrics.render().decode()
        assert "fs2_serve_sync_wait_saturated_total 28.0" in rendered
    finally:
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)
    assert admission.health()["sync_waiters"] == 0


@pytest.mark.asyncio
async def test_one_worker_completes_success_then_processes_second_claim(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient())
    await admission.start()
    try:
        first = await admission.admit(principal, request("worker-first-0001"))
        second = await admission.admit(principal, request("worker-second-0002"))
        first_done = await wait_status(store, first.id, {OperationStatus.SUCCEEDED})
        second_done = await wait_status(store, second.id, {OperationStatus.SUCCEEDED})
        assert first_done.attempt == second_done.attempt == 1
        assert first_done.completed_at is not None
        assert second_done.completed_at is not None
        assert second_done.completed_at >= first_done.completed_at
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_static_hot_route_skips_activation_intent(registry, cipher, hasher) -> None:
    model = registry.get("qwen3-8b")
    static_model = replace(
        model,
        gateway=replace(
            model.gateway,
            binding=replace(model.binding, activation=replace(model.binding.activation, enabled=False)),
        ),
        lean_static=True,
    )
    static_registry = type(registry)(registry.catalog, {static_model.id: static_model})
    store = MemoryStore(cipher, hasher, auto_activate=False)
    principal = await setup_principal(store)
    admission = service(static_registry, store, StubRuntimeClient())
    await admission.start()
    try:
        operation = await admission.admit(principal, request("static-hot-no-activation-0001"))
        final = await wait_status(store, operation.id, {OperationStatus.SUCCEEDED})
        assert final.attempt == 1
        assert store.activation_intents == {}
    finally:
        await admission.close()


class PreemptOnceRuntime(StubRuntimeClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def invoke(self, model, operation, request_body):
        self.calls += 1
        if self.calls == 1:
            raise PreemptedError("provider response contained private diagnostics")
        return await super().invoke(model, operation, request_body)


class UpstreamFailureRuntime(StubRuntimeClient):
    async def invoke(self, model, operation, request_body):
        del model, operation, request_body
        return RuntimeResult(
            status_code=400,
            body=b"UPSTREAM_FAILURE_BODY_MUST_NOT_BE_PERSISTED",
            content_type="application/json",
            elapsed_seconds=0.01,
            runtime=RuntimeIdentity(gpu_count=1),
            semantic_outcome="not_evaluated",
            failure_code="upstream_http_error",
        )


@pytest.mark.asyncio
async def test_upstream_failure_persists_only_metadata_not_failure_body(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(registry, store, UpstreamFailureRuntime())
    await admission.start()
    try:
        operation = await admission.admit(principal, request("failure-metadata-key-0001"))
        final = await wait_status(store, operation.id, {OperationStatus.FAILED})
        row = store.operations[operation.id]
        assert final.error_code == "upstream_http_error"
        assert final.result_available is False
        assert row.response is None and row.response_hmac is None
        assert b"UPSTREAM_FAILURE_BODY_MUST_NOT_BE_PERSISTED" not in repr(row).encode()
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_retry_keeps_operation_id_increments_fence_and_charges_each_attempt(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    runtime = PreemptOnceRuntime()
    admission = service(registry, store, runtime)
    await admission.start()
    try:
        original = await admission.admit(principal, request("preempt-retry-0001"))
        final = await wait_status(store, original.id, {OperationStatus.SUCCEEDED})
        assert final.id == original.id
        assert final.attempt == 2
        assert final.fencing_token >= 3
        assert final.estimated_gpu_seconds == 10
        token = (await store.token_for_verification(principal.token_id))[0]  # type: ignore[index]
        assert token.gpu_seconds_used == 10
        assert token.gpu_seconds_reserved == 0
    finally:
        await admission.close()


class SlowRuntime(StubRuntimeClient):
    def __init__(self) -> None:
        super().__init__()
        self.invoked = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def invoke(self, model, operation, request_body):
        del model, operation, request_body
        self.invoked.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class LoseHeartbeatStore(MemoryStore):
    async def heartbeat(self, operation_id, *, worker_id, fencing_token, lease_seconds):
        del worker_id, fencing_token, lease_seconds
        operation = await self.get_operation(operation_id)
        await self.revoke_token(operation.token_id, actor="emergency-revoker")
        from fs2_serve.store import StaleLeaseError

        raise StaleLeaseError("lease deliberately lost")


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_work_and_fence_blocks_late_completion(registry, cipher, hasher) -> None:
    store = LoseHeartbeatStore(cipher, hasher)
    principal = await setup_principal(store)
    runtime = SlowRuntime()
    admission = service(registry, store, runtime, lease=0.15)
    await admission.start()
    try:
        operation = await admission.admit(principal, request("heartbeat-loss-0001"))
        await asyncio.wait_for(runtime.invoked.wait(), timeout=1)
        cancelled = await wait_status(store, operation.id, {OperationStatus.CANCELLED})
        await asyncio.wait_for(runtime.cancelled.wait(), timeout=1)
        assert cancelled.error_code == "token_revoked"
        assert cancelled.fencing_token >= 2
        await asyncio.sleep(0.05)
        assert (await store.get_operation(operation.id)).status == OperationStatus.CANCELLED
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_deadline_aborts_live_work_and_maintenance_expires_the_lease(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    runtime = SlowRuntime()
    admission = service(registry, store, runtime, lease=0.15)
    await admission.start()
    try:
        operation = await admission.admit(
            principal,
            request("deadline-live-0001", deadline_at=datetime.now(UTC) + timedelta(seconds=0.08)),
        )
        await asyncio.wait_for(runtime.invoked.wait(), timeout=1)
        final = await wait_status(store, operation.id, {OperationStatus.EXPIRED})
        assert final.error_code in {"deadline_exceeded", "lease_recovery_exhausted"}
        assert final.reserved_gpu_seconds == 0
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_graceful_shutdown_stops_claiming_cancels_work_and_requeues_with_fence(registry, cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    runtime = SlowRuntime()
    admission = service(registry, store, runtime, lease=30, grace=0.03)
    await admission.start()
    operation = await admission.admit(principal, request("shutdown-release-0001"))
    await asyncio.wait_for(runtime.invoked.wait(), timeout=1)
    await admission.close()
    current = await store.get_operation(operation.id)
    assert current.status == OperationStatus.QUEUED
    assert current.error_code == "worker_released"
    assert current.fencing_token >= 2
    assert runtime.cancelled.is_set()


class TrackingAdmission(AdmissionService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.heartbeat_started = asyncio.Event()
        self.heartbeat_cancelled = asyncio.Event()

    async def _heartbeat(self, operation) -> None:
        del operation
        self.heartbeat_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.heartbeat_cancelled.set()
            raise


class OrderedReleaseStore(MemoryStore):
    def __init__(self, *args, runtime_cancelled: asyncio.Event, heartbeat_cancelled: asyncio.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.runtime_cancelled = runtime_cancelled
        self.heartbeat_cancelled = heartbeat_cancelled
        self.release_seen = asyncio.Event()

    async def release_operation(self, operation_id, *, worker_id, fencing_token):
        assert self.runtime_cancelled.is_set()
        assert self.heartbeat_cancelled.is_set()
        released = await super().release_operation(operation_id, worker_id=worker_id, fencing_token=fencing_token)
        self.release_seen.set()
        return released


@pytest.mark.asyncio
async def test_shutdown_awaits_runtime_and_heartbeat_before_fenced_release_and_leaves_no_children(
    registry, cipher, hasher
) -> None:
    runtime = SlowRuntime()
    heartbeat_cancelled = asyncio.Event()
    store = OrderedReleaseStore(
        cipher,
        hasher,
        runtime_cancelled=runtime.cancelled,
        heartbeat_cancelled=heartbeat_cancelled,
    )
    principal = await setup_principal(store)
    admission = TrackingAdmission(
        registry=registry,
        store=store,
        runtime=runtime,
        metrics=Metrics(registry.list(enabled_only=True)),
        worker_concurrency=1,
        poll_seconds=0.01,
        lease_seconds=30,
        maintenance_interval_seconds=0.02,
        shutdown_grace_seconds=0.03,
    )
    store.heartbeat_cancelled = admission.heartbeat_cancelled
    await admission.start()
    operation = await admission.admit(principal, request("shutdown-order-key-0001"))
    await asyncio.wait_for(runtime.invoked.wait(), timeout=1)
    await asyncio.wait_for(admission.heartbeat_started.wait(), timeout=1)
    await admission.close()

    assert store.release_seen.is_set()
    assert runtime.cancelled.is_set() and admission.heartbeat_cancelled.is_set()
    assert (await store.get_operation(operation.id)).status == OperationStatus.QUEUED
    child_prefixes = (f"fs2-operation-{operation.id}", f"fs2-heartbeat-{operation.id}")
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith(child_prefixes)]


class FlakyLoopStore(MemoryStore):
    def __init__(self, *args, claim_failures: int, janitor_failures: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.claim_failures = claim_failures
        self.janitor_failures = janitor_failures
        self.claim_calls = 0
        self.janitor_calls = 0
        self.deadline_janitor_calls = 0

    async def claim_operation(self, worker_id, *, lease_seconds):
        self.claim_calls += 1
        if self.claim_failures:
            self.claim_failures -= 1
            raise RuntimeError("claim transport detail must not terminate supervisor")
        return await super().claim_operation(worker_id, lease_seconds=lease_seconds)

    async def reap_stale_operations(self):
        self.janitor_calls += 1
        if self.janitor_failures:
            self.janitor_failures -= 1
            raise RuntimeError("janitor transport detail must not terminate supervisor")
        return await super().reap_stale_operations()

    async def expire_deadline_operations(self):
        self.deadline_janitor_calls += 1
        return await super().expire_deadline_operations()


class RuntimeSupervisorBoundaryStore(MemoryStore):
    """Reject destructive retention if an API replica ever crosses the role boundary."""

    async def purge_expired_payloads(self) -> int:
        raise AssertionError("gateway runtime must not run destructive payload retention")

    async def delete_expired_rows(
        self,
        *,
        operation_retention_seconds: int,
        token_retention_seconds: int,
        audit_retention_seconds: int,
        usage_retention_seconds: int,
    ) -> dict[str, int]:
        del operation_retention_seconds, token_retention_seconds, audit_retention_seconds, usage_retention_seconds
        raise AssertionError("gateway runtime must not delete durable facts")


@pytest.mark.asyncio
async def test_gateway_supervisor_never_runs_maintenance_retention(registry, cipher, hasher) -> None:
    store = RuntimeSupervisorBoundaryStore(cipher, hasher)
    admission = service(registry, store, StubRuntimeClient())
    await admission.start()
    try:
        async with asyncio.timeout(2):
            while admission.health()["ready"] is not True:
                await asyncio.sleep(0.01)
        assert admission.health()["janitor_healthy"] is True
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_claim_and_janitor_supervisors_recover_and_report_health(registry, cipher, hasher) -> None:
    store = FlakyLoopStore(cipher, hasher, claim_failures=2, janitor_failures=2)
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient())
    await admission.start()
    assert admission.health()["ready"] is False
    try:
        operation = await admission.admit(principal, request("supervisor-recovery-key-0001"))
        await wait_status(store, operation.id, {OperationStatus.SUCCEEDED})
        async with asyncio.timeout(2):
            while admission.health()["ready"] is not True:
                await asyncio.sleep(0.01)
        health = admission.health()
        assert health["workers_healthy"] is True
        assert health["janitor_healthy"] is True
        assert store.claim_calls >= 3 and store.janitor_calls >= 3 and store.deadline_janitor_calls >= 3
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_loop_failure_backoff_is_bounded_and_health_stays_unready(registry, cipher, hasher) -> None:
    store = FlakyLoopStore(cipher, hasher, claim_failures=100, janitor_failures=100)
    admission = service(registry, store, StubRuntimeClient())
    await admission.start()
    try:
        await asyncio.sleep(0.18)
        health = admission.health()
        assert health["ready"] is False
        assert health["workers_healthy"] is False
        assert health["janitor_healthy"] is False
        assert 2 <= store.claim_calls <= 4
        assert 2 <= store.janitor_calls <= 4
    finally:
        await admission.close()


class FailingExecutionRuntime(StubRuntimeClient):
    async def activate(self, model, operation):
        del model, operation
        raise AssertionError("runtime details must not escape the worker boundary")


class FlakyReleaseStore(MemoryStore):
    def __init__(self, *args, release_failures: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.release_failures = release_failures
        self.release_calls = 0
        self.claim_calls = 0
        self.release_resolved = False
        self.claimed_before_release = False

    async def claim_operation(self, worker_id, *, lease_seconds):
        self.claim_calls += 1
        if self.release_calls and not self.release_resolved:
            self.claimed_before_release = True
        return await super().claim_operation(worker_id, lease_seconds=lease_seconds)

    async def release_operation(self, operation_id, *, worker_id, fencing_token):
        self.release_calls += 1
        if self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("database transport unavailable")
        released = await super().release_operation(operation_id, worker_id=worker_id, fencing_token=fencing_token)
        self.release_resolved = True
        return released


class FailOnceExecutionRuntime(StubRuntimeClient):
    def __init__(self) -> None:
        super().__init__()
        self.activation_calls = 0

    async def activate(self, model, operation):
        self.activation_calls += 1
        if self.activation_calls == 1:
            raise AssertionError("runtime details must not escape the worker boundary")
        await super().activate(model, operation)


async def claimed_failure_fixture(registry, cipher, hasher, *, release_failures: int):
    store = FlakyReleaseStore(cipher, hasher, release_failures=release_failures)
    principal = await setup_principal(store)
    admission = service(registry, store, FailingExecutionRuntime())
    operation = await admission.admit(principal, request(f"release-retry-key-{release_failures:04d}"))
    claimed = await store.claim_operation("release-worker", lease_seconds=30)
    assert claimed is not None
    return store, admission, operation, claimed


@pytest.mark.asyncio
async def test_fenced_release_retries_then_recovers_without_orphaning_claim(registry, cipher, hasher) -> None:
    store, admission, operation, claimed = await claimed_failure_fixture(registry, cipher, hasher, release_failures=2)

    await admission._run_claim("release-worker", claimed)

    current = await store.get_operation(operation.id)
    assert current.status == OperationStatus.QUEUED
    assert store.release_calls == 3
    assert admission.live() is True
    assert not admission._inflight


@pytest.mark.asyncio
async def test_supervisor_release_fails_then_recovers_before_claiming_again(registry, cipher, hasher) -> None:
    store = FlakyReleaseStore(cipher, hasher, release_failures=2)
    principal = await setup_principal(store)
    runtime = FailOnceExecutionRuntime()
    admission = service(registry, store, runtime, lease=30)
    await admission.start()
    try:
        operation = await admission.admit(principal, request("supervised-release-recovery-0001"))
        final = await wait_status(store, operation.id, {OperationStatus.SUCCEEDED}, timeout=4)

        assert final.attempt == 2
        assert runtime.activation_calls == 2
        assert store.release_calls == 3
        assert store.claim_calls >= 2
        assert store.claimed_before_release is False
        assert admission.live() is True
        assert not admission._inflight
    finally:
        await admission.close()


@pytest.mark.asyncio
async def test_permanent_fenced_release_failure_marks_liveness_failed_for_restart(registry, cipher, hasher) -> None:
    store, admission, operation, claimed = await claimed_failure_fixture(registry, cipher, hasher, release_failures=100)

    await admission._run_claim("release-worker", claimed)

    assert store.release_calls == 6
    assert admission.live() is False
    assert admission.health()["fatal_workers"] == 1
    assert "release-worker" in admission._inflight
    assert (await store.get_operation(operation.id)).status == OperationStatus.ACTIVATING


@pytest.mark.asyncio
async def test_supervisor_permanent_release_failure_stops_claims_and_requires_restart(registry, cipher, hasher) -> None:
    store = FlakyReleaseStore(cipher, hasher, release_failures=100)
    principal = await setup_principal(store)
    admission = service(registry, store, FailingExecutionRuntime(), lease=30)
    await admission.start()
    try:
        first = await admission.admit(principal, request("supervised-release-fatal-0001"))
        second = await admission.admit(principal, request("supervised-release-fatal-0002"))
        async with asyncio.timeout(4):
            while admission.live():
                await asyncio.sleep(0.01)

        assert store.release_calls == 6
        assert store.claim_calls == 1
        assert store.claimed_before_release is False
        assert admission.health()["fatal_workers"] == 1
        assert admission.health()["workers_healthy"] is False
        assert len(admission._inflight) == 1
        assert next(iter(admission._inflight.values())).id == first.id
        assert (await store.get_operation(first.id)).status == OperationStatus.ACTIVATING
        assert (await store.get_operation(second.id)).status == OperationStatus.QUEUED
    finally:
        await admission.close()
