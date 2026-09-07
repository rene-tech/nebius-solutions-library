"""Real-PostgreSQL enforcement of the operator scientific model dispatch policy.

Every test runs against a live PostgreSQL instance: the durable outcome is the
stored batch row, the stored policy row, and what two independent controller
identities actually do with them. Fakes stand in only for Kubernetes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from scientific_batch_fakes import FakeScientificBatchCluster

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, Scope, TokenCreate
from fs2_serve.postgres import PostgresStore
from fs2_serve.scientific_admin import (
    ScientificModelPolicyInvalidError,
    ScientificModelPolicyStaleRevisionError,
    ScientificModelSnapshot,
    ScientificRunQuery,
)
from fs2_serve.scientific_admin_models import ScientificModelPolicyUpdate, ScientificModelReadinessList
from fs2_serve.scientific_admin_postgres import (
    PostgresScientificModelPolicyAdminAdapter,
    PostgresScientificRunAdminAdapter,
)
from fs2_serve.scientific_batch.models import (
    AttemptArtifactCommit,
    AttemptOutcome,
    BatchStatus,
    CheckpointMode,
    LifecyclePhase,
    PreemptionMode,
    ResourceClass,
    SchedulingAdmission,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
    StageStatus,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
    attempt_identity,
    workload_name,
)
from fs2_serve.scientific_batch.policy import (
    PolicyAwareScientificBatchController,
    PostgresScientificModelPolicyRepository,
    ScientificDispatchHeldError,
    ScientificModelPolicyStaleError,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

CONTROL_ROOT = Path(__file__).parents[1]
TENANT = "tenant-oncology"
OTHER_TENANT = "tenant-translational"
MODEL = "rfdiffusion"
PRIORITIES = {
    ServiceClass.PRESENTATION: ("scientific-presentation", 1000),
    ServiceClass.INTERACTIVE: ("scientific-interactive", 800),
    ServiceClass.CUSTOMER_BATCH: ("scientific-customer-batch", 500),
    ServiceClass.BULK_BACKFILL: ("scientific-bulk-backfill", 100),
}
PHASES = (
    LifecyclePhase.ADMITTED,
    LifecyclePhase.NODE_PENDING,
    LifecyclePhase.IMAGE_LOADING,
    LifecyclePhase.ARTIFACT_LOADING,
    LifecyclePhase.ACTIVE_COMPUTE,
    LifecyclePhase.TEARDOWN,
)


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
        await connection.execute(
            "TRUNCATE fs2_operations,fs2_tokens,fs2_scientific_model_policies,fs2_audit_events RESTART IDENTITY CASCADE"
        )
    try:
        yield connected
    finally:
        async with connected.pool.acquire() as connection:
            await connection.execute(
                "TRUNCATE fs2_operations,fs2_tokens,fs2_scientific_model_policies,fs2_audit_events "
                "RESTART IDENTITY CASCADE"
            )
        await connected.close()


async def principal_of(store: PostgresStore, tenant_id: str = TENANT) -> Principal:
    token_id = uuid4()
    prefix = f"fs2_pat_{token_id.hex[:12]}"
    await store.issue_token(
        token_id=token_id,
        prefix=prefix,
        pepper_key_id="pepper-v1",
        digest="argon2-test-digest",
        request=TokenCreate(
            principal_id=f"scientist-{tenant_id}",
            tenant_id=tenant_id,
            scopes={Scope.INFERENCE_INVOKE},
            models={MODEL},
            max_concurrency=8,
        ),
        created_by=f"researcher-{tenant_id}",
    )
    return Principal(
        token_id=token_id,
        token_prefix=prefix,
        principal_id=f"scientist-{tenant_id}",
        tenant_id=tenant_id,
        scopes=frozenset({Scope.INFERENCE_INVOKE.value}),
        models=frozenset({MODEL}),
        max_concurrency=8,
    )


async def durable_input_artifact(
    store: PostgresStore, operation_id: UUID, *, artifact_id: UUID, tenant_id: str
) -> None:
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


def scheduling_for(service_class: ServiceClass, captured_at: datetime) -> SchedulingSnapshot:
    priority_class, priority = PRIORITIES[service_class]
    return SchedulingSnapshot(
        policy_revision=hashlib.sha256(b"scientific-model-policy-test").hexdigest(),
        captured_at=captured_at,
        service_class=service_class,
        tenant_queue="scientific",
        model_lane=MODEL,
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id="design",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific",
                workload_priority_class=priority_class,
                workload_priority_value=priority,
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


PLAN = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="design", max_attempts=2),))


async def admit_batch(
    store: PostgresStore,
    principal: Principal,
    *,
    idempotency_key: str,
    service_class: ServiceClass = ServiceClass.CUSTOMER_BATCH,
) -> UUID:
    operation = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id=MODEL,
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
    await durable_input_artifact(store, operation.id, artifact_id=artifact_id, tenant_id=principal.tenant_id)
    await PostgresScientificBatchRepository(store.pool).create(
        operation_id=operation.id,
        tenant_id=principal.tenant_id,
        model_id=MODEL,
        variant_id="rfdiffusion-h100",
        input_artifact_id=artifact_id,
        plan=PLAN,
        scheduling=scheduling_for(service_class, operation.accepted_at),
    )
    return operation.id


class FakeArtifactLifecycle:
    """Minimal canonical-artifact stand-in: every succeeded attempt has a valid commit."""

    async def open_attempt(self, resource: WorkloadResource, *, started_at: datetime) -> None:
        del resource, started_at

    async def close_attempt(self, state: ScientificBatchState, attempt: ScientificAttemptState) -> None:
        del state, attempt

    async def ensure_stage_commit(self, state: ScientificBatchState, *, stage_id: str) -> None:
        del state, stage_id

    async def artifact_commits(
        self, state: ScientificBatchState, *, stage_id: str
    ) -> tuple[AttemptArtifactCommit, ...]:
        now = datetime.now(UTC)
        return tuple(
            AttemptArtifactCommit(
                operation_id=state.operation_id,
                stage_id=stage_id,
                attempt_ids=(attempt.attempt_id,),
                logical_artifact_id=f"{stage_id}-result",
                handoff_artifact_id=uuid4(),
                handoff_digest="sha256:" + hashlib.sha256(str(attempt.attempt_id).encode()).hexdigest(),
                handoff_size_bytes=100,
                handoff_media_type="application/json",
                handoff_compression=None,
                manifest_artifact_id=uuid4(),
                validation_artifact_id=uuid4(),
                manifest_digest="sha256:" + "1" * 64,
                validation_digest="sha256:" + "2" * 64,
                committed_at=now,
                validated_at=now,
                semantic_valid=True,
            )
            for attempt in state.stage(stage_id).attempts
            if attempt.outcome is AttemptOutcome.SUCCEEDED
        )


def controller_for(
    store: PostgresStore,
    cluster: FakeScientificBatchCluster,
    *,
    controller_id: str,
) -> PolicyAwareScientificBatchController:
    return PolicyAwareScientificBatchController(
        repository=PostgresScientificBatchRepository(store.pool),
        cluster=cluster,
        controller_id=controller_id,
        namespace="fs2-models",
        artifact_lifecycle=FakeArtifactLifecycle(),
        lease_seconds=30,
    )


def observe_success(cluster: FakeScientificBatchCluster, attempt: ScientificAttemptState) -> None:
    resource = cluster.resources[cluster.key(attempt.workload)]
    decision = resource.scheduling
    cluster.set_observation(
        attempt.workload,
        WorkloadObservation(
            ref=attempt.workload,
            attempt_id=attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=PHASES,
            scheduling_admission=SchedulingAdmission(
                resolved_pool_id=decision.resolved_pool_preference[0],
                admitted_resource_flavor="inference-h100-1x",
                accelerator_resource_name=decision.accelerator_resource_name,
                accelerator_count=decision.accelerator_count,
                admitted_at=datetime.now(UTC),
            ),
        ),
    )


async def statuses(store: PostgresStore, *operation_ids: UUID) -> list[str]:
    async with store.pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT operation_id,status FROM fs2_scientific_batches WHERE operation_id=ANY($1::uuid[])",
            list(operation_ids),
        )
    by_id = {row["operation_id"]: str(row["status"]) for row in rows}
    return [by_id[operation_id] for operation_id in operation_ids]


async def drain_to_success(
    store: PostgresStore,
    cluster: FakeScientificBatchCluster,
    controller: PolicyAwareScientificBatchController,
    operation_id: UUID,
    *,
    tenant_id: str = TENANT,
) -> None:
    """Complete one running batch through the ordinary observe -> succeed path."""

    batches = PostgresScientificBatchRepository(store.pool)
    running = await batches.get(operation_id, tenant_id=tenant_id)
    assert running.status is BatchStatus.RUNNING
    observe_success(cluster, running.stage("design").attempts[-1])
    for _ in range(6):
        if (await batches.get(operation_id, tenant_id=tenant_id)).status is BatchStatus.SUCCEEDED:
            return
        await controller.reconcile_once()
    assert (await batches.get(operation_id, tenant_id=tenant_id)).status is BatchStatus.SUCCEEDED


class CatalogModels:
    """Global catalog reader stand-in: the model id set the policy service validates against."""

    def __init__(self, model_ids: tuple[str, ...] = (MODEL,)) -> None:
        self.model_ids = model_ids

    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=[], projection_issues=[]),
            observed_at=datetime.now(UTC),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_creates_policy_table_with_least_privilege_runtime_grants(store: PostgresStore) -> None:
    async with store.pool.acquire() as connection:
        for privilege in ("SELECT", "INSERT", "UPDATE"):
            assert await connection.fetchval(
                "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_model_policies',$1)",
                privilege,
            )
        for privilege in ("DELETE", "TRUNCATE"):
            assert not await connection.fetchval(
                "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_model_policies',$1)",
                privilege,
            )
        assert await connection.fetchval(
            "SELECT has_function_privilege('fs2_serve_runtime','fs2_scientific_dispatch_hold(text,text)','EXECUTE')"
        )
        for role in ("fs2_serve_reporting", "fs2_serve_maintenance", "fs2_serve_activation"):
            assert not await connection.fetchval(
                "SELECT has_table_privilege($1,'fs2_scientific_model_policies','SELECT')",
                role,
            )
        # The predicate is open for a model without any policy row.
        assert await connection.fetchval("SELECT fs2_scientific_dispatch_hold('rfdiffusion','tenant-x')") is None
        # The runtime role can evaluate it and write policy rows, but not erase them.
        await connection.execute("SET ROLE fs2_serve_runtime")
        assert await connection.fetchval("SELECT fs2_scientific_dispatch_hold('rfdiffusion','tenant-x')") is None
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("DELETE FROM fs2_scientific_model_policies WHERE false")
        await connection.execute("RESET ROLE")
        # Revision and scope are guarded in SQL, not only by the repository.
        await connection.execute(
            "INSERT INTO fs2_scientific_model_policies(model_id,tenant_id,revision,paused,updated_by) "
            "VALUES('rfdiffusion',NULL,1,true,'guard-test')"
        )
        with pytest.raises(asyncpg.RaiseError, match="advance by exactly one"):
            await connection.execute("UPDATE fs2_scientific_model_policies SET revision=5 WHERE model_id='rfdiffusion'")
        with pytest.raises(asyncpg.RaiseError, match="scope is immutable"):
            await connection.execute(
                "UPDATE fs2_scientific_model_policies SET revision=2,tenant_id='tenant-x' WHERE model_id='rfdiffusion'"
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO fs2_scientific_model_policies(model_id,tenant_id,revision,paused,updated_by) "
                "VALUES('rfdiffusion',NULL,1,false,'guard-test')"
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pause_holds_queued_work_durably_while_running_work_drains_and_resume_dispatches(
    store: PostgresStore,
) -> None:
    principal = await principal_of(store)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    batches = PostgresScientificBatchRepository(store.pool)
    cluster = FakeScientificBatchCluster()
    controller = controller_for(store, cluster, controller_id="controller-a")

    running_id = await admit_batch(store, principal, idempotency_key="policy-pause-running")
    assert await controller.reconcile_once() == running_id
    assert (await batches.get(running_id, tenant_id=TENANT)).status is BatchStatus.RUNNING

    paused = await policies.set(
        MODEL,
        tenant_id=None,
        expected_revision=0,
        paused=True,
        max_active_runs=None,
        reason="customer PoC window closed",
        actor="operator-ada",
    )
    assert paused.policy is not None and paused.policy.revision == 1 and paused.dispatch_state == "paused"
    queued_id = await admit_batch(store, principal, idempotency_key="policy-pause-queued")

    # The queued batch is not claimable, but the running one still reconciles.
    claimed = await batches.claim_next(controller_id="controller-b", lease_seconds=30, now=datetime.now(UTC))
    assert claimed is not None and claimed.operation_id == running_id
    await batches.release(claimed)
    assert await statuses(store, queued_id) == ["queued"]
    assert len(cluster.apply_history) == 1

    # Running work drains to its own terminal state under the pause, and its
    # public Operation is projected exactly as without a policy.
    await drain_to_success(store, cluster, controller, running_id)
    assert (await store.get_operation(running_id, tenant_id=TENANT)).status is OperationStatus.SUCCEEDED
    # With nothing but the held batch left, a poll finds no claim and creates nothing.
    assert await controller.reconcile_once() is None
    assert await statuses(store, queued_id) == ["queued"]
    assert len(cluster.apply_history) == 1

    # The held batch is still visible to its owner and cancellable while paused.
    view = await policies.get(MODEL, tenant_id=None)
    assert (view.counts.queued, view.counts.running) == (1, 0)
    cancel_target = await admit_batch(store, principal, idempotency_key="policy-pause-cancel")
    await batches.request_cancel(cancel_target, tenant_id=TENANT, actor="scientist")
    assert await controller.reconcile_once() == cancel_target
    assert await statuses(store, cancel_target) == ["cancelled"]

    resumed = await policies.set(
        MODEL,
        tenant_id=None,
        expected_revision=1,
        paused=False,
        max_active_runs=None,
        reason=None,
        actor="operator-ada",
    )
    assert resumed.policy is not None and resumed.policy.revision == 2 and resumed.dispatch_state == "open"
    assert await controller.reconcile_once() == queued_id
    assert await statuses(store, queued_id) == ["running"]
    assert len(cluster.apply_history) == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrency_one_dispatches_two_runs_strictly_one_at_a_time(store: PostgresStore) -> None:
    principal = await principal_of(store)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="operator-ada"
    )
    first = await admit_batch(store, principal, idempotency_key="policy-cap-first")
    second = await admit_batch(store, principal, idempotency_key="policy-cap-second")
    cluster = FakeScientificBatchCluster()
    controller = controller_for(store, cluster, controller_id="controller-a")

    assert await controller.reconcile_once() == first
    assert await statuses(store, first, second) == ["running", "queued"]
    # The second is held: repeated polls reconcile only the running batch.
    for _ in range(3):
        assert await controller.reconcile_once() == first
    assert await statuses(store, first, second) == ["running", "queued"]
    assert len(cluster.apply_history) == 1
    view = await policies.get(MODEL, tenant_id=None)
    assert view.dispatch_state == "at-limit" and (view.counts.queued, view.counts.running) == (1, 1)

    await drain_to_success(store, cluster, controller, first)
    assert await controller.reconcile_once() == second
    assert await statuses(store, first, second) == ["succeeded", "running"]
    assert len(cluster.apply_history) == 2
    view = await policies.get(MODEL, tenant_id=None)
    assert view.dispatch_state == "at-limit" and (view.counts.queued, view.counts.running) == (0, 1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_controller_owners_racing_the_same_cap_admit_exactly_one(store: PostgresStore) -> None:
    principal = await principal_of(store)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="operator-ada"
    )
    first = await admit_batch(store, principal, idempotency_key="policy-race-first")
    second = await admit_batch(store, principal, idempotency_key="policy-race-second")
    batches = PostgresScientificBatchRepository(store.pool)

    # Both replicas hold a claim on a different queued batch before either
    # commits, exactly the window the claim-time filter cannot close alone.
    claim_a = await batches.claim_next(controller_id="replica-a", lease_seconds=30, now=datetime.now(UTC))
    claim_b = await batches.claim_next(controller_id="replica-b", lease_seconds=30, now=datetime.now(UTC))
    assert claim_a is not None and claim_b is not None
    assert {claim_a.operation_id, claim_b.operation_id} == {first, second}

    def dispatching(state: ScientificBatchState) -> ScientificBatchState:
        from dataclasses import replace

        stage = state.stage("design")
        shard_id = state.plan.stage("design").workload_units[0]
        attempt = ScientificAttemptState(
            attempt_id=attempt_identity(state.operation_id, "design", shard_id, 1),
            stage_id="design",
            shard_id=shard_id,
            attempt_number=1,
            workload=WorkloadRef(
                namespace="fs2-models",
                name=workload_name(state.operation_id, "design", shard_id, 1),
                kind=WorkloadKind.JOB,
                route_namespace="fs2-models",
            ),
            started_at=datetime.now(UTC),
        )
        started = replace(stage, status=StageStatus.ACTIVE, attempts=(attempt,))
        return replace(state, stages=(started,), status=BatchStatus.RUNNING, revision=state.revision + 1)

    state_a = await batches.load(claim_a)
    state_b = await batches.load(claim_b)
    outcomes = await asyncio.gather(
        batches.replace(claim_a, expected_revision=0, record=dispatching(state_a), events=(), now=datetime.now(UTC)),
        batches.replace(claim_b, expected_revision=0, record=dispatching(state_b), events=(), now=datetime.now(UTC)),
        return_exceptions=True,
    )
    held = [outcome for outcome in outcomes if isinstance(outcome, ScientificDispatchHeldError)]
    written = [outcome for outcome in outcomes if isinstance(outcome, ScientificBatchState)]
    assert len(held) == 1 and held[0].reason == "concurrency"
    assert len(written) == 1 and written[0].status is BatchStatus.RUNNING
    assert sorted(await statuses(store, first, second)) == ["queued", "running"]
    await batches.release(claim_a)
    await batches.release(claim_b)

    # The same race through two whole controller replicas sharing one cluster:
    # the raised cap admits exactly one more batch, the other hold is quiet,
    # and no reconcile fails.
    await policies.set(
        MODEL, tenant_id=None, expected_revision=1, paused=False, max_active_runs=2, reason=None, actor="operator-ada"
    )
    third = await admit_batch(store, principal, idempotency_key="policy-race-third")
    fourth = await admit_batch(store, principal, idempotency_key="policy-race-fourth")
    cluster = FakeScientificBatchCluster()
    controller_a = controller_for(store, cluster, controller_id="replica-a")
    controller_b = controller_for(store, cluster, controller_id="replica-b")
    results = await asyncio.gather(controller_a.reconcile_once(), controller_b.reconcile_once())
    assert set(results) <= {first, second, third, fourth}
    for _ in range(4):
        await asyncio.gather(controller_a.reconcile_once(), controller_b.reconcile_once())
    final = await statuses(store, first, second, third, fourth)
    assert final.count("running") == 2 and final.count("queued") == 2
    # One apply materializes the raced record's pending attempt, one dispatches
    # the single newly admitted batch; the held batches created nothing.
    assert len(cluster.apply_history) == 2
    async with store.pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_scientific_batches WHERE model_id=$1 AND status='running'", MODEL
            )
            == 2
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_held_queue_dispatches_by_frozen_kueue_priority_then_acceptance_order(store: PostgresStore) -> None:
    principal = await principal_of(store)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=True, max_active_runs=1, reason=None, actor="operator-ada"
    )
    bulk = await admit_batch(
        store, principal, idempotency_key="policy-prio-bulk", service_class=ServiceClass.BULK_BACKFILL
    )
    batch_first = await admit_batch(store, principal, idempotency_key="policy-prio-batch-1")
    interactive = await admit_batch(
        store, principal, idempotency_key="policy-prio-interactive", service_class=ServiceClass.INTERACTIVE
    )
    batch_second = await admit_batch(store, principal, idempotency_key="policy-prio-batch-2")
    cluster = FakeScientificBatchCluster()
    controller = controller_for(store, cluster, controller_id="controller-a")
    assert await controller.reconcile_once() is None

    await policies.set(
        MODEL, tenant_id=None, expected_revision=1, paused=False, max_active_runs=1, reason=None, actor="operator-ada"
    )
    order: list[UUID] = []
    for _ in range(4):
        dispatched = await controller.reconcile_once()
        assert dispatched is not None
        order.append(dispatched)
        await drain_to_success(store, cluster, controller, dispatched)
    assert order == [interactive, batch_first, batch_second, bulk]
    assert await controller.reconcile_once() is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_tenant_scope_layers_below_the_all_tenants_policy(store: PostgresStore) -> None:
    oncology = await principal_of(store, TENANT)
    translational = await principal_of(store, OTHER_TENANT)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    await policies.set(
        MODEL,
        tenant_id=TENANT,
        expected_revision=0,
        paused=True,
        max_active_runs=None,
        reason="hold",
        actor="tenant-op",
    )
    held = await admit_batch(store, oncology, idempotency_key="policy-tenant-held")
    free = await admit_batch(store, translational, idempotency_key="policy-tenant-free")
    cluster = FakeScientificBatchCluster()
    controller = controller_for(store, cluster, controller_id="controller-a")
    assert await controller.reconcile_once() == free
    assert await controller.reconcile_once() == free
    assert await statuses(store, held, free) == ["queued", "running"]

    tenant_view = await policies.get(MODEL, tenant_id=TENANT)
    assert tenant_view.dispatch_state == "paused" and tenant_view.inherited is None
    assert (tenant_view.counts.queued, tenant_view.counts.running) == (1, 0)
    assert (tenant_view.all_tenants_counts.queued, tenant_view.all_tenants_counts.running) == (1, 1)
    global_view = await policies.get(MODEL, tenant_id=None)
    assert global_view.policy is None and global_view.dispatch_state == "open"

    # An all-tenants cap of 1 now also holds the other tenant's next batch, and
    # a tenant scope reports the inherited row it cannot override upwards.
    await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="operator-ada"
    )
    second_free = await admit_batch(store, translational, idempotency_key="policy-tenant-free-2")
    assert await controller.reconcile_once() == free
    assert await statuses(store, second_free) == ["queued"]
    other_view = await policies.get(MODEL, tenant_id=OTHER_TENANT)
    assert other_view.policy is None and other_view.inherited is not None
    assert other_view.dispatch_state == "at-limit" and other_view.effective_max_active_runs == 1
    listed = await policies.list(tenant_id=OTHER_TENANT, model_ids=("boltzgen",))
    assert [view.model_id for view in listed] == ["boltzgen", MODEL]
    assert listed[0].policy is None and listed[0].dispatch_state == "open"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_and_racing_policy_revisions_are_rejected_and_audited(store: PostgresStore) -> None:
    policies = PostgresScientificModelPolicyRepository(store.pool)
    first = await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=True, max_active_runs=None, reason=None, actor="operator-a"
    )
    assert first.policy is not None and first.policy.revision == 1
    with pytest.raises(ScientificModelPolicyStaleError) as stale:
        await policies.set(
            MODEL, tenant_id=None, expected_revision=0, paused=False, max_active_runs=3, reason=None, actor="operator-b"
        )
    assert stale.value.current_revision == 1
    with pytest.raises(ScientificModelPolicyStaleError):
        await policies.set(
            MODEL, tenant_id=None, expected_revision=2, paused=False, max_active_runs=3, reason=None, actor="operator-b"
        )
    unchanged = await policies.get(MODEL, tenant_id=None)
    assert unchanged.policy is not None and unchanged.policy.paused and unchanged.policy.revision == 1

    # Two operators racing from the same revision: exactly one wins.
    outcomes = await asyncio.gather(
        policies.set(
            MODEL, tenant_id=None, expected_revision=1, paused=False, max_active_runs=2, reason="a", actor="operator-a"
        ),
        policies.set(
            MODEL, tenant_id=None, expected_revision=1, paused=False, max_active_runs=4, reason="b", actor="operator-b"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, ScientificModelPolicyStaleError) for outcome in outcomes) == 1
    winner = await policies.get(MODEL, tenant_id=None)
    assert winner.policy is not None and winner.policy.revision == 2 and winner.policy.max_active_runs in {2, 4}

    # Racing creations of a new scope row: the unique scope index turns the
    # loser into a stale-revision rejection rather than a second row.
    outcomes = await asyncio.gather(
        policies.set(
            MODEL, tenant_id=TENANT, expected_revision=0, paused=True, max_active_runs=None, reason=None, actor="a"
        ),
        policies.set(
            MODEL, tenant_id=TENANT, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="b"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, ScientificModelPolicyStaleError) for outcome in outcomes) == 1
    async with store.pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM fs2_scientific_model_policies WHERE model_id=$1", MODEL)
            == 2
        )
        audit = await connection.fetch(
            "SELECT actor,tenant_id,target_type,target_id,outcome,detail FROM fs2_audit_events "
            "WHERE action='scientific_model_policy.set' ORDER BY id"
        )
    assert len(audit) == 3
    assert {row["target_id"] for row in audit} == {MODEL}
    assert all(row["target_type"] == "scientific_model" and row["outcome"] == "applied" for row in audit)
    assert audit[0]["tenant_id"] is None and audit[-1]["tenant_id"] == TENANT

    # The admin adapter maps the repository conflict to the service's typed error.
    adapter = PostgresScientificModelPolicyAdminAdapter(repository=policies)
    with pytest.raises(ScientificModelPolicyStaleRevisionError) as admin_stale:
        await adapter.set_policy(
            MODEL,
            tenant_id=None,
            update=ScientificModelPolicyUpdate(expected_revision=1, paused=False, max_active_runs=None, reason=None),
            actor="operator-c",
        )
    assert admin_stale.value.current_revision == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_projection_reports_effective_policy_counts_and_held_run_reason(store: PostgresStore) -> None:
    principal = await principal_of(store)
    policies = PostgresScientificModelPolicyRepository(store.pool)
    batches = PostgresScientificBatchRepository(store.pool)
    cluster = FakeScientificBatchCluster()
    controller = controller_for(store, cluster, controller_id="controller-a")
    running = await admit_batch(store, principal, idempotency_key="policy-admin-running")
    assert await controller.reconcile_once() == running
    await policies.set(
        MODEL, tenant_id=None, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="operator-ada"
    )
    queued = await admit_batch(store, principal, idempotency_key="policy-admin-queued")
    assert await controller.reconcile_once() == running

    adapter = PostgresScientificModelPolicyAdminAdapter(repository=policies)
    snapshot = await adapter.list_policies(tenant_id=None, model_ids=(MODEL, "boltzgen"))
    items = {item.model_id: item for item in snapshot.data.items}
    assert set(items) == {MODEL, "boltzgen"}
    policy = items[MODEL]
    assert policy.catalog_known and policy.desired.revision == 1 and policy.desired.max_active_runs == 1
    assert policy.effective.state == "at-limit" and policy.effective.max_active_runs == 1
    assert (policy.counts.queued, policy.counts.running) == (1, 1)
    assert policy.counts == policy.all_tenants_counts
    assert policy.enforcement.preemptive is False and policy.enforcement.running_work_drains is True
    assert items["boltzgen"].desired.revision == 0 and items["boltzgen"].effective.state == "open"

    class Models:
        async def list_models(self, *, tenant_id: str | None = None):  # noqa: ANN202
            from test_scientific_admin_postgres import ModelAdapter

            return await ModelAdapter().list_models(tenant_id=tenant_id)

    runs = PostgresScientificRunAdminAdapter(pool=store.pool, batches=batches, models=Models())
    listed = await runs.list_runs(
        ScientificRunQuery(
            from_at=datetime.now(UTC) - timedelta(minutes=5),
            to_at=datetime.now(UTC) + timedelta(minutes=1),
            tenant_id=TENANT,
        )
    )
    by_id = {item.id: item for item in listed.data.items}
    held_run = by_id[str(queued)]
    assert held_run.status == "queued" and held_run.queue.admission_state == "pending"
    assert "active-run cap" in held_run.queue.admission_reason
    assert by_id[str(running)].status == "running"
    assert "held" not in by_id[str(running)].queue.admission_reason

    detail = await runs.get_run(queued, tenant_id=TENANT)
    assert "active-run cap" in detail.data.run.queue.admission_reason
    await policies.set(
        MODEL, tenant_id=None, expected_revision=1, paused=True, max_active_runs=1, reason="maintenance", actor="op"
    )
    paused_detail = await runs.get_run(queued, tenant_id=TENANT)
    assert "paused by operator policy" in paused_detail.data.run.queue.admission_reason


async def test_startup_choice_inherits_preserves_and_resets_per_scope(store: PostgresStore) -> None:
    policies = PostgresScientificModelPolicyRepository(store.pool)
    snapshot = {"sample-structure": {"backend": "cuda-criu", "bundle_id": "protenix-qualified"}}
    normal = {"sample-structure": {"backend": "normal-load", "bundle_id": None}}

    async def put(tenant: str | None, revision: int, **kwargs):
        return await policies.set(
            MODEL,
            tenant_id=tenant,
            expected_revision=revision,
            paused=False,
            max_active_runs=None,
            reason=None,
            actor="test-operator",
            **kwargs,
        )

    await put(None, 0, startup_policies=snapshot)
    assert await policies.startup_policies(model_id=MODEL, tenant_id=TENANT) == snapshot
    await put(TENANT, 0, startup_policies=normal)
    assert await policies.startup_policies(model_id=MODEL, tenant_id=TENANT) == normal
    assert await policies.startup_policies(model_id=MODEL, tenant_id=OTHER_TENANT) == snapshot
    await put(TENANT, 1)  # Older dispatch-only clients preserve startup choice.
    assert await policies.startup_policies(model_id=MODEL, tenant_id=TENANT) == normal
    await put(TENANT, 2, startup_policies={})
    assert await policies.startup_policies(model_id=MODEL, tenant_id=TENANT) == snapshot
    await put(None, 1, startup_policies={})
    assert await policies.startup_policies(model_id=MODEL, tenant_id=TENANT) == {}


async def test_admin_startup_choice_validates_and_publishes_available_bundles(store: PostgresStore) -> None:
    policies = PostgresScientificModelPolicyRepository(store.pool)
    options = {"sample-structure": ["protenix-qualified"]}

    def validate(*, model_id, overrides):
        assert model_id == MODEL
        for stage, choice in overrides.items():
            if stage not in options or choice["bundle_id"] not in options[stage]:
                raise ValueError("snapshot bundle is not qualified for this stage")
        return overrides

    adapter = PostgresScientificModelPolicyAdminAdapter(
        repository=policies,
        startup_validator=validate,
        startup_options=lambda **_: options,
    )
    update = ScientificModelPolicyUpdate(
        expected_revision=0,
        paused=False,
        startup_policies={"sample-structure": {"backend": "cuda-criu", "bundle_id": "protenix-qualified"}},
    )
    view = await adapter.set_policy(MODEL, tenant_id=None, update=update, actor="test-operator")
    assert view.startup_options == options
    assert view.desired.startup_policies["sample-structure"].bundle_id == "protenix-qualified"
    invalid = update.model_copy(update={"expected_revision": 1})
    invalid.startup_policies["sample-structure"].bundle_id = "wrong-model-bundle"
    with pytest.raises(ScientificModelPolicyInvalidError, match="not qualified"):
        await adapter.set_policy(MODEL, tenant_id=None, update=invalid, actor="test-operator")
    assert (await policies.startup_policies(model_id=MODEL, tenant_id=TENANT))["sample-structure"][
        "bundle_id"
    ] == "protenix-qualified"
