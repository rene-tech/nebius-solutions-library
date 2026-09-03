"""Real-PostgreSQL durability of scientific-batch cancellation and legacy state.

Every test here runs against a live PostgreSQL 16 instance so the durable
outcome is the stored row and its projected Operation, not an in-memory fake.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from scientific_batch_fakes import FakeScientificBatchCluster

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, Scope, TokenCreate
from fs2_serve.postgres import PostgresStore
from fs2_serve.scientific_batch.codec import state_from_value
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.models import (
    LEGACY_ADMISSION_FAILURE_CODE,
    PREVIOUS_STATE_SCHEMA,
    STATE_SCHEMA,
    AttemptOutcome,
    BatchEventKind,
    BatchStatus,
    CheckpointMode,
    LifecyclePhase,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
    StageStatus,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_batch.protocols import BatchFenceLostError

CONTROL_ROOT = Path(__file__).parents[1]
FIXTURES = CONTROL_ROOT / "tests/fixtures"
TENANT = "tenant-oncology"
LEGACY_ROWS = {
    "pending": "scientific-batch-state-v7-545d71d9.json",
    "active": "scientific-batch-state-v7-active-aaaaaaaa.json",
    "complete": "scientific-batch-state-v7-complete-cccccccc.json",
}


@pytest_asyncio.fixture
async def store() -> PostgresStore:
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    connected = await PostgresStore.connect(
        database_url,
        CONTROL_ROOT / "migrations",
        PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"p" * 32}),
        KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"h" * 32}),
        payload_ttl_seconds=3600,
    )
    await connected.migrate()
    async with connected.pool.acquire() as connection:
        await connection.execute("TRUNCATE fs2_operations,fs2_tokens RESTART IDENTITY CASCADE")
    try:
        yield connected
    finally:
        async with connected.pool.acquire() as connection:
            await connection.execute("TRUNCATE fs2_operations,fs2_tokens RESTART IDENTITY CASCADE")
        await connected.close()


async def principal_of(store: PostgresStore) -> Principal:
    token_id = uuid4()
    prefix = f"fs2_pat_{token_id.hex[:12]}"
    await store.issue_token(
        token_id=token_id,
        prefix=prefix,
        pepper_key_id="pepper-v1",
        digest="argon2-test-digest",
        request=TokenCreate(
            principal_id="scientist-ada",
            tenant_id=TENANT,
            scopes={Scope.INFERENCE_INVOKE},
            models={"rfdiffusion"},
            max_concurrency=1,
        ),
        created_by="researcher-ada",
    )
    return Principal(
        token_id=token_id,
        token_prefix=prefix,
        principal_id="scientist-ada",
        tenant_id=TENANT,
        scopes=frozenset({Scope.INFERENCE_INVOKE.value}),
        models=frozenset({"rfdiffusion"}),
        max_concurrency=1,
    )


async def durable_input_artifact(
    store: PostgresStore,
    operation_id: UUID,
    *,
    artifact_id: UUID,
    tenant_id: str = TENANT,
) -> None:
    """Insert the input artifact the batch row's foreign key requires."""

    attempt_id = uuid4()
    digest = "sha256:" + hashlib.sha256(str(artifact_id).encode()).hexdigest()
    async with store.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_scientific_stage_attempts(
                attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,
                status,started_at,retention_expires_at
            ) VALUES($1,$2,$3,'input','-',1,'running',clock_timestamp(),clock_timestamp()+interval '1 day')
            """,
            attempt_id,
            operation_id,
            tenant_id,
        )
        await connection.execute(
            """
            INSERT INTO fs2_scientific_artifacts(
                id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,size_bytes,
                media_type,storage_key,access_profile,retention_expires_at
            ) VALUES($1,$2,$3,$4,'input','-','input',$5,1,'application/json',$6,'public',
                clock_timestamp()+interval '1 day')
            """,
            artifact_id,
            attempt_id,
            operation_id,
            tenant_id,
            digest,
            f"scientific/v1/tenants/{tenant_id}/operations/{operation_id}/stages/input/shards/-/"
            f"attempts/{attempt_id}/input/sha256/{digest.removeprefix('sha256:')}",
        )


async def admit_batch(
    store: PostgresStore,
    principal: Principal,
    *,
    idempotency_key: str,
) -> tuple[UUID, PostgresScientificBatchRepository]:
    operation = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="rfdiffusion",
            operation="generate-backbone",
            protocol="scientific-batch-v1",
            idempotency_key=idempotency_key,
            request_body=b'{"schema":"fs2-serve.nebius.ai/scientific-run-request/v1"}',
        ),
        model_revision="2" * 40,
        reserved_gpu_seconds=0,
        max_attempts=1,
    )
    artifact_id = uuid4()
    await durable_input_artifact(store, operation.id, artifact_id=artifact_id)
    plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="design", max_attempts=2),))
    scheduling = SchedulingSnapshot(
        policy_revision=hashlib.sha256(b"scientific-batch-postgres-state").hexdigest(),
        captured_at=operation.accepted_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="scientific",
        model_lane="rfdiffusion",
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id="design",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific",
                workload_priority_class="scientific-customer-batch",
                workload_priority_value=500,
                resolved_pool_preference=("h100-preemptible",),
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                max_queue_seconds=None,
                max_execution_seconds=None,
                checkpoint_mode=CheckpointMode.RESTART,
                preemption_mode=PreemptionMode.RESTARTABLE,
            ),
        ),
    )
    batches = PostgresScientificBatchRepository(store.pool)
    await batches.create(
        operation_id=operation.id,
        tenant_id=TENANT,
        model_id="rfdiffusion",
        variant_id="rfdiffusion-h100",
        input_artifact_id=artifact_id,
        plan=plan,
        scheduling=scheduling,
    )
    return operation.id, batches


def controller_for(
    batches: PostgresScientificBatchRepository,
    cluster: FakeScientificBatchCluster,
    *,
    controller_id: str = "controller-a",
    result_publisher: object | None = None,
) -> ScientificBatchController:
    return ScientificBatchController(
        repository=batches,
        cluster=cluster,
        controller_id=controller_id,
        namespace="fs2-models",
        result_publisher=result_publisher,  # type: ignore[arg-type]
        lease_seconds=30,
    )


async def stored_row(store: PostgresStore, operation_id: UUID) -> asyncpg.Record:
    async with store.pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status,revision,cancel_requested,state FROM fs2_scientific_batches WHERE operation_id=$1",
            operation_id,
        )
    assert row is not None
    return row


async def insert_legacy_row(store: PostgresStore, principal: Principal, name: str) -> ScientificBatchState:
    """Insert one exact pre-v8 row, exactly as a released controller left it."""

    legacy = json.loads((FIXTURES / name).read_text())
    operation_id = UUID(legacy["operation_id"])
    artifact_id = UUID(legacy["input_artifact_id"])
    decoded = state_from_value(legacy)
    assert decoded.stored_schema == PREVIOUS_STATE_SCHEMA
    async with store.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_operations(
                id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,operation,
                idempotency_key,request_hmac_key_id,request_hmac,request_content_type,
                payload_expires_at,max_attempts,status
            ) VALUES($1,$2,$3,$4,$5,'7cd4ace1','scientific-batch-v1','design',$6,'hmac-v1',$7,
                'application/json',clock_timestamp()+interval '1 day',2,$8::fs2_operation_status)
            """,
            operation_id,
            decoded.tenant_id,
            principal.principal_id,
            principal.token_id,
            decoded.model_id,
            f"legacy-{name}",
            "8" * 64,
            decoded.status.value,
        )
    await durable_input_artifact(store, operation_id, artifact_id=artifact_id, tenant_id=decoded.tenant_id)
    async with store.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_scientific_batches(
                operation_id,batch_id,workload_id,tenant_id,model_id,variant_id,
                input_artifact_id,scheduling_digest,status,revision,cancel_requested,state
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
            """,
            operation_id,
            UUID(legacy["batch_id"]),
            UUID(legacy["workload_id"]),
            decoded.tenant_id,
            decoded.model_id,
            decoded.variant_id,
            artifact_id,
            "sha256:" + "0" * 64,
            decoded.status.value,
            decoded.revision,
            decoded.cancel_requested,
            json.dumps(legacy, sort_keys=True, separators=(",", ":")),
        )
    return decoded


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_cancellation_settles_in_one_reconcile_and_fences_a_stale_claim(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    operation_id, batches = await admit_batch(store, principal, idempotency_key="scientific-cancel-0001")
    cluster = FakeScientificBatchCluster()
    controller = controller_for(batches, cluster)
    assert await controller.reconcile_once() == operation_id
    running = await batches.get(operation_id, tenant_id=TENANT)
    assert running.status is BatchStatus.RUNNING
    attempt = running.stage("design").attempts[0]

    cancelled_request = await batches.request_cancel(operation_id, tenant_id=TENANT, actor=principal.principal_id)
    assert cancelled_request.cancel_requested is True
    assert cancelled_request.revision == running.revision

    assert await controller.reconcile_once() == operation_id
    terminal = await batches.get(operation_id, tenant_id=TENANT)
    assert terminal.status is BatchStatus.CANCELLED
    assert terminal.failure_code == "cancelled"
    assert terminal.stage("design").status is StageStatus.CANCELLED
    released = terminal.stage("design").attempts[0]
    assert released.outcome is AttemptOutcome.CANCELLED
    assert released.deletion_requested and released.resource_released
    assert cluster.delete_history == [attempt.workload]

    row = await stored_row(store, operation_id)
    assert row["status"] == "cancelled" and row["cancel_requested"] is True
    assert json.loads(row["state"])["schema_version"] == STATE_SCHEMA

    projected = await store.get_operation(operation_id, tenant_id=TENANT)
    assert projected.status is OperationStatus.CANCELLED and projected.error_code == "cancelled"
    events = await batches.list_events(operation_id, tenant_id=TENANT)
    assert events[-1].draft.kind is BatchEventKind.BATCH_CANCELLED
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)

    # The terminal row is fenced: the claim that produced it can no longer
    # write, and no controller can claim it back into a running state.
    stale = await batches.claim_next(controller_id="controller-a", lease_seconds=30, now=datetime.now(UTC))
    assert stale is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_restart_resumes_a_durable_mid_cancel_deletion(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    operation_id, batches = await admit_batch(store, principal, idempotency_key="scientific-cancel-restart-0001")
    cluster = FakeScientificBatchCluster()
    first_controller = controller_for(batches, cluster, controller_id="controller-before-restart")
    assert await first_controller.reconcile_once() == operation_id
    running = await batches.get(operation_id, tenant_id=TENANT)
    attempt = running.stage("design").attempts[0]
    cluster.deletion_polls_before_absent[cluster.key(attempt.workload)] = 1
    await batches.request_cancel(operation_id, tenant_id=TENANT, actor=principal.principal_id)

    # The accepted DELETE is one durable write, while UID-specific absence is
    # deliberately still false.  This is the exact crash/restart boundary: the
    # stored row must not claim that Kubernetes released quota or GPUs.
    assert await first_controller.reconcile_once() == operation_id
    deleting = await batches.get(operation_id, tenant_id=TENANT)
    persisted_attempt = deleting.stage("design").attempts[0]
    assert deleting.status is BatchStatus.RUNNING
    assert persisted_attempt.deletion_requested is True
    assert persisted_attempt.resource_released is False
    row = await stored_row(store, operation_id)
    encoded = json.loads(row["state"])
    encoded_attempt = encoded["stages"][0]["attempts"][0]
    assert encoded_attempt["deletion_requested"] is True
    assert encoded_attempt["resource_released"] is False
    assert cluster.delete_history == [attempt.workload]

    # A fresh controller identity reopens the row, polls the same workload UID,
    # and completes teardown without issuing another DELETE or creating work.
    restarted = controller_for(batches, cluster, controller_id="controller-after-restart")
    assert await restarted.reconcile_once() == operation_id
    terminal = await batches.get(operation_id, tenant_id=TENANT)
    assert terminal.status is BatchStatus.CANCELLED
    settled_attempt = terminal.stage("design").attempts[0]
    assert settled_attempt.resource_released is True
    assert settled_attempt.last_phase is LifecyclePhase.TEARDOWN
    assert cluster.delete_history == [attempt.workload]
    assert len(cluster.apply_history) == 1
    events = await batches.list_events(operation_id, tenant_id=TENANT)
    assert [event.draft.kind for event in events].count(BatchEventKind.BATCH_CANCELLED) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_cancel_during_a_held_claim_is_carried_into_the_fenced_write(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    operation_id, batches = await admit_batch(store, principal, idempotency_key="scientific-cancel-0002")
    claim = await batches.claim_next(controller_id="controller-a", lease_seconds=30, now=datetime.now(UTC))
    assert claim is not None
    held = await batches.load(claim)
    await batches.request_cancel(operation_id, tenant_id=TENANT, actor=principal.principal_id)

    # The holder committed a transition that predates the cancellation. The
    # level-triggered request must survive it rather than being overwritten.
    from dataclasses import replace as dataclass_replace

    written = await batches.replace(
        claim,
        expected_revision=held.revision,
        record=dataclass_replace(held, revision=held.revision + 1),
        events=(),
        now=datetime.now(UTC),
    )
    assert written.cancel_requested is True

    async with store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_scientific_batches SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            operation_id,
        )
    superseded = await batches.claim_next(controller_id="controller-b", lease_seconds=30, now=datetime.now(UTC))
    assert superseded is not None and superseded.fencing_token > claim.fencing_token
    with pytest.raises(BatchFenceLostError):
        await batches.load(claim)
    await batches.release(superseded)

    controller = controller_for(batches, FakeScientificBatchCluster(), controller_id="controller-b")
    assert await controller.reconcile_once() == operation_id
    assert (await batches.get(operation_id, tenant_id=TENANT)).status is BatchStatus.CANCELLED


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_duplicate_reconcile_and_restart_add_no_state_or_events(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    operation_id, batches = await admit_batch(store, principal, idempotency_key="scientific-restart-0001")
    cluster = FakeScientificBatchCluster()
    assert await controller_for(batches, cluster).reconcile_once() == operation_id
    await batches.request_cancel(operation_id, tenant_id=TENANT, actor=principal.principal_id)
    assert await controller_for(batches, cluster).reconcile_once() == operation_id

    settled = await stored_row(store, operation_id)
    events = await batches.list_events(operation_id, tenant_id=TENANT)
    deletes = list(cluster.delete_calls)

    # A restarted controller, and a second replica under another identity, both
    # reopen the same durable row and must add nothing to it.
    for controller_id in ("controller-a", "controller-b", "controller-restarted"):
        assert await controller_for(batches, cluster, controller_id=controller_id).reconcile_once() is None
    repeated = await stored_row(store, operation_id)
    assert (repeated["status"], repeated["revision"]) == (settled["status"], settled["revision"])
    assert await batches.list_events(operation_id, tenant_id=TENANT) == events
    assert cluster.delete_calls == deletes
    async with store.pool.acquire() as connection:
        operation_events = await connection.fetch(
            "SELECT event,status FROM fs2_operation_events WHERE operation_id=$1 ORDER BY id",
            operation_id,
        )
    assert [row["event"] for row in operation_events].count("scientific_batch_cancelled") == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_terminal_result_publication_is_durable_and_exactly_once(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    operation_id, batches = await admit_batch(store, principal, idempotency_key="scientific-terminal-0001")

    class Publisher:
        def __init__(self) -> None:
            self.calls: list[UUID] = []

        async def publish_terminal(self, state: ScientificBatchState) -> None:
            assert state.status is BatchStatus.CANCELLED
            assert state.result_published is False
            self.calls.append(state.operation_id)
            if len(self.calls) == 1:
                raise RuntimeError("injected artifact result failure")

    publisher = Publisher()
    cluster = FakeScientificBatchCluster()
    controller = controller_for(batches, cluster, result_publisher=publisher)
    assert await controller.reconcile_once() == operation_id
    await batches.request_cancel(operation_id, tenant_id=TENANT, actor=principal.principal_id)
    assert await controller.reconcile_once() == operation_id

    cancelled = await batches.get(operation_id, tenant_id=TENANT)
    assert cancelled.status is BatchStatus.CANCELLED and cancelled.result_published is False
    projected = await store.get_operation(operation_id, tenant_id=TENANT)
    assert projected.status is OperationStatus.CANCELLED

    with pytest.raises(RuntimeError, match="injected artifact result failure"):
        await controller.reconcile_once()
    assert (await batches.get(operation_id, tenant_id=TENANT)).result_published is False

    assert await controller.reconcile_once() == operation_id
    published = await batches.get(operation_id, tenant_id=TENANT)
    assert published.result_published is True
    assert publisher.calls == [operation_id, operation_id]

    # The terminal row is complete, so it leaves the claim set for good.
    assert await controller.reconcile_once() is None
    assert (await batches.get(operation_id, tenant_id=TENANT)) == published
    assert (await store.get_operation(operation_id, tenant_id=TENANT)).status is OperationStatus.CANCELLED


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "active"])
async def test_real_postgres_retires_an_open_legacy_row_without_running_it(
    store: PostgresStore,
    kind: str,
) -> None:
    principal = await principal_of(store)
    legacy = await insert_legacy_row(store, principal, LEGACY_ROWS[kind])
    cluster = FakeScientificBatchCluster()
    controller = controller_for(batches := PostgresScientificBatchRepository(store.pool), cluster)

    # The base controller reopened this row and raised out of the reconcile
    # loop: a pending row hit a bare StopIteration looking for a stage binding
    # the pre-v8 admission never froze, and an active row went looking for a
    # workload no live cluster ever had.
    assert await controller.reconcile_once() == legacy.operation_id

    retired = await batches.get(legacy.operation_id, tenant_id=legacy.tenant_id)
    assert retired.status is BatchStatus.FAILED
    assert retired.failure_code == LEGACY_ADMISSION_FAILURE_CODE
    assert retired.result_published is True
    assert all(attempt.resource_released for stage in retired.stages for attempt in stage.attempts)
    assert not any(stage.status is StageStatus.ACTIVE for stage in retired.stages)
    assert cluster.apply_history == []

    row = await stored_row(store, legacy.operation_id)
    assert row["status"] == "failed"
    assert json.loads(row["state"])["schema_version"] == STATE_SCHEMA
    projected = await store.get_operation(legacy.operation_id, tenant_id=legacy.tenant_id)
    assert projected.status is OperationStatus.FAILED
    assert projected.error_code == LEGACY_ADMISSION_FAILURE_CODE
    events = await batches.list_events(legacy.operation_id, tenant_id=legacy.tenant_id)
    assert events[-1].draft.kind is BatchEventKind.BATCH_FAILED
    assert events[-1].draft.code == LEGACY_ADMISSION_FAILURE_CODE

    # Retirement is terminal and idempotent; nothing reclaims the row.
    assert await controller.reconcile_once() is None
    assert (await stored_row(store, legacy.operation_id))["revision"] == row["revision"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_postgres_keeps_a_completed_legacy_row_readable_and_untouched(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    legacy = await insert_legacy_row(store, principal, LEGACY_ROWS["complete"])
    batches = PostgresScientificBatchRepository(store.pool)
    controller = controller_for(batches, FakeScientificBatchCluster())

    assert await controller.reconcile_once() is None
    readable = await batches.get(legacy.operation_id, tenant_id=legacy.tenant_id)
    assert readable == legacy
    assert readable.status is BatchStatus.SUCCEEDED
    assert readable.stored_schema == PREVIOUS_STATE_SCHEMA
    assert readable.legacy_admission is True
    assert await batches.list_events(legacy.operation_id, tenant_id=legacy.tenant_id) == []

    row = await stored_row(store, legacy.operation_id)
    assert row["status"] == "succeeded" and row["revision"] == legacy.revision
    assert json.loads(row["state"])["schema_version"] == PREVIOUS_STATE_SCHEMA
