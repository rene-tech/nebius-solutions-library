from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.models import AdmissionRequest, Principal, Scope, TokenCreate
from fs2_serve.postgres import PostgresStore
from fs2_serve.scientific_admin import ScientificModelSnapshot, ScientificRunQuery
from fs2_serve.scientific_admin_models import ScientificModelReadiness, ScientificModelReadinessList
from fs2_serve.scientific_admin_postgres import (
    PostgresScientificArtifactAdminAdapter,
    PostgresScientificRunAdminAdapter,
    postgres_scientific_admin_read_service,
)
from fs2_serve.scientific_artifacts import (
    PostgresArtifactRepository,
    RunResultRecord,
    ScientificArtifactService,
)
from fs2_serve.scientific_batch.codec import state_to_value
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    BatchEvent,
    BatchEventDraft,
    BatchEventKind,
    BatchStatus,
    LifecyclePhase,
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
    WorkloadRef,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_run_result import ScientificRunResult

NOW = datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
OPERATION_ID = UUID("018f0f3a-0f9b-7ccd-8d87-6e5201c95001")


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _state(
    operation_id: UUID = OPERATION_ID,
    *,
    captured_at: datetime = NOW - timedelta(minutes=2),
    input_artifact_id: UUID | None = None,
) -> ScientificBatchState:
    plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="design"),))
    scheduling = SchedulingSnapshot(
        policy_revision=_digest("policy"),
        captured_at=captured_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="tenant-oncology",
        model_lane="rfdiffusion",
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id="design",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific-batch",
                workload_priority_class="scientific-customer-batch",
                workload_priority_value=500,
                resolved_pool_preference=("h100-preemptible",),
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=plan.stages[0].checkpoint_mode,
                preemption_mode=plan.stages[0].preemption_mode,
            ),
        ),
    )
    admitted = ScientificBatchState.admit(
        operation_id=operation_id,
        tenant_id="tenant-oncology",
        model_id="rfdiffusion",
        variant_id="rfdiffusion-h100",
        input_artifact_id=input_artifact_id or uuid4(),
        plan=plan,
        scheduling=scheduling,
    )
    attempt = ScientificAttemptState(
        attempt_id=uuid4(),
        stage_id="design",
        shard_id="main",
        attempt_number=1,
        workload=WorkloadRef(
            namespace="fs2-models",
            name="scientific-design-1",
            kind=WorkloadKind.JOB,
            uid="job-uid-1",
        ),
        started_at=NOW - timedelta(minutes=1),
        outcome=AttemptOutcome.ACTIVE,
        last_phase=LifecyclePhase.ACTIVE_COMPUTE,
        scheduling_admission=SchedulingAdmission(
            resolved_pool_id="h100-preemptible",
            admitted_resource_flavor="inference-h100-1x",
            accelerator_resource_name="nvidia.com/gpu",
            accelerator_count=1,
            admitted_at=NOW - timedelta(seconds=50),
        ),
        kueue_workload_uid="kueue-workload-uid-1",
        pod_uids=("pod-uid-1",),
    )
    return replace(
        admitted,
        status=BatchStatus.RUNNING,
        stages=(replace(admitted.stages[0], status=StageStatus.ACTIVE, attempts=(attempt,)),),
    )


def _model() -> ScientificModelReadiness:
    return ScientificModelReadiness.model_validate(
        {
            "model_id": "rfdiffusion",
            "display_name": "RFdiffusion",
            "readiness": "candidate",
            "readiness_reason": "Candidate runtime.",
            "execution_mode": "scientific-batch",
            "batch_supported": True,
            "interactive_supported": False,
            "service_classes": ["customer-batch", "bulk-backfill"],
            "backend": {
                "backend_id": "rfdiffusion:native-upstream",
                "kind": "containerized-scientific-runtime",
                "source_repository": "https://github.com/RosettaCommons/RFdiffusion",
                "source_revision": "1" * 40,
                "model_revision": None,
                "runtime_image_digest": None,
                "execution_identity_digest": None,
            },
            "access": {
                "profile": "standard",
                "state": "not-required",
                "gate": "No restricted academic asset is required by this backend.",
                "receipt_digest": None,
                "credentials_exposed": False,
                "alternative": None,
            },
            "caching": {
                "exact_tier": "not-observed",
                "image": "candidate",
                "artifacts": "candidate",
                "reference_data": "unsupported",
                "runtime_checkpoint": "unavailable",
                "gpu_snapshot": "unavailable",
                "reason": "No exact fast-start observation is available.",
            },
        }
    )


class ModelAdapter:
    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=[_model()]),
            observed_at=NOW,
        )


class FakeConnection:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.queries: list[str] = []

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        self.queries.append(query)
        return NOW

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        del args
        self.queries.append(query)
        return [self.record]

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any]:
        del args
        self.queries.append(query)
        return self.record


class Acquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        del args


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> Acquire:
        return Acquire(self.connection)


class BatchRepository:
    def __init__(self, events: tuple[BatchEvent, ...] = ()) -> None:
        self.events = events

    async def list_events(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[BatchEvent]:
        assert operation_id == OPERATION_ID
        assert tenant_id == "tenant-oncology"
        assert after_sequence == 0
        assert limit == 1000
        return list(self.events)


def _record(state: ScientificBatchState) -> dict[str, Any]:
    return {
        "id": OPERATION_ID,
        "tenant_id": state.tenant_id,
        "principal_id": "svc-cd8-design",
        "model_id": state.model_id,
        "model_revision": "2" * 40,
        "operation": "generate-backbone",
        "accepted_at": NOW - timedelta(minutes=2),
        "completed_at": None,
        "token_prefix": "fs2_pat_7c91",
        "created_by": "researcher-ada",
        "state": state_to_value(state),
        "updated_at": NOW,
        "admitted_at": NOW - timedelta(seconds=20),
        "cancel_requested_at": None,
        "cancel_actor": None,
    }


def _event(state: ScientificBatchState, sequence: int, phase: LifecyclePhase, offset: int) -> BatchEvent:
    attempt = state.stages[0].attempts[0]
    return BatchEvent(
        sequence=sequence,
        occurred_at=NOW + timedelta(seconds=offset),
        draft=BatchEventDraft.build(
            operation_id=state.operation_id,
            batch_id=state.batch_id,
            workload_id=state.workload_id,
            kind=BatchEventKind.LIFECYCLE,
            stage_id="design",
            shard_id="main",
            attempt_id=attempt.attempt_id,
            phase=phase,
        ),
    )


async def test_postgres_run_list_projects_real_controller_rows_without_guessing_gpu_time() -> None:
    state = _state()
    connection = FakeConnection(_record(state))
    adapter = PostgresScientificRunAdminAdapter(
        pool=cast(Any, FakePool(connection)),
        batches=cast(Any, BatchRepository()),
        models=ModelAdapter(),
    )

    result = await adapter.list_runs(
        ScientificRunQuery(
            from_at=NOW - timedelta(hours=1),
            to_at=NOW + timedelta(seconds=1),
        )
    )

    assert result.observed_at == NOW
    assert result.data.items[0].attribution.user_id == "researcher-ada"
    assert result.data.items[0].service_class.effective == "customer-batch"
    assert result.data.items[0].gpu_accounting.active.evidence == "unavailable"
    assert result.data.items[0].gpu_accounting.active.value is None
    sql = "\n".join(connection.queries)
    assert "fs2_scientific_batches" in sql
    assert "request_ciphertext" not in sql


async def test_postgres_run_detail_uses_controller_events_for_closed_phase_durations() -> None:
    state = _state()
    events = (
        _event(state, 1, LifecyclePhase.QUEUED, -15),
        _event(state, 2, LifecyclePhase.SCHEDULING, -10),
        _event(state, 3, LifecyclePhase.ACTIVE_COMPUTE, -7),
        _event(state, 4, LifecyclePhase.TEARDOWN, 0),
    )
    adapter = PostgresScientificRunAdminAdapter(
        pool=cast(Any, FakePool(FakeConnection(_record(state)))),
        batches=cast(Any, BatchRepository(events)),
        models=ModelAdapter(),
    )

    result = await adapter.get_run(OPERATION_ID, tenant_id="tenant-oncology")

    assert result.data.run.queue.admission_state == "admitted"
    assert result.data.stages[0].attempts[0].job_uid == "job-uid-1"
    active = next(item for item in result.data.lifecycle_phases if item.phase == "active-compute")
    assert active.duration.value == 7
    assert active.duration.evidence == "measured"
    assert result.data.run.cancellation.grace_seconds is None


def test_production_factory_binds_postgres_controller_and_artifact_adapters() -> None:
    connection = FakeConnection(_record(_state()))
    service = postgres_scientific_admin_read_service(
        pool=cast(Any, FakePool(connection)),
        registry=cast(Any, object()),
        catalog_dir=Path(__file__).parents[3] / "catalog/runtime",
        artifact_service=None,
        scientific_batches=None,
        source_max_age_seconds=90,
        adapter_timeout_seconds=2,
    )

    assert isinstance(service.runs, PostgresScientificRunAdminAdapter)
    assert service.artifacts.__class__.__name__ == "PostgresScientificArtifactAdminAdapter"


async def test_artifact_adapter_projects_the_canonical_terminal_result_without_signed_handles() -> None:
    document = json.loads(
        (
            Path(__file__).parents[3] / "catalog/runtime/contracts/examples/scientific-run-result.example.json"
        ).read_text()
    )
    document["operation_id"] = str(OPERATION_ID)
    result = ScientificRunResult.model_validate(document)
    record = RunResultRecord(
        operation_id=OPERATION_ID,
        tenant_id="tenant-oncology",
        result=result,
        result_digest=result.digest,
        committed_at=NOW,
        retention_expires_at=NOW + timedelta(days=90),
    )

    class ResultService:
        async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord:
            assert operation_id == OPERATION_ID
            assert tenant_id == "tenant-oncology"
            return record

    adapter = PostgresScientificArtifactAdminAdapter(cast(Any, ResultService()))
    snapshot = await adapter.for_operation(OPERATION_ID, tenant_id="tenant-oncology")

    assert snapshot.terminal_status == "succeeded"
    assert snapshot.service_class == "customer-batch"
    assert snapshot.attempts[0].gpu_count == 1
    assert snapshot.attempts[0].pod_count == 1
    assert snapshot.attempts[0].resolved_pool_id == "gpu-preemptible"
    assert snapshot.attempts[0].admitted_resource_flavor == "gpu-preemptible"
    assert snapshot.attempts[0].accelerator_resource_name == "nvidia.com/gpu"
    assert snapshot.attempts[0].admitted_at == datetime(2026, 9, 2, 0, 1, tzinfo=UTC)
    assert snapshot.semantic_validation.receipt_digest == "sha256:" + "2" * 64
    assert {artifact.artifact_id for artifact in snapshot.artifacts} == {
        "manifest.input.01",
        "manifest.output.01",
    }
    assert all(not artifact.download.available for artifact in snapshot.artifacts)


@pytest_asyncio.fixture
async def postgres_admin_store() -> PostgresStore:
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(
        database_url,
        Path(__file__).parents[1] / "migrations",
        PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"p" * 32}),
        KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"h" * 32}),
        payload_ttl_seconds=3600,
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


@pytest.mark.postgres
async def test_real_postgres_admin_projection_reads_durable_controller_and_key_attribution(
    postgres_admin_store: PostgresStore,
) -> None:
    token_id = uuid4()
    token_prefix = f"fs2_pat_{token_id.hex[:12]}"
    await postgres_admin_store.issue_token(
        token_id=token_id,
        prefix=token_prefix,
        pepper_key_id="pepper-v1",
        digest="argon2-test-digest",
        request=TokenCreate(
            principal_id="scientist-ada",
            tenant_id="tenant-oncology",
            scopes={Scope.INFERENCE_INVOKE},
            models={"rfdiffusion"},
            max_concurrency=1,
        ),
        created_by="researcher-ada",
    )
    principal = Principal(
        token_id=token_id,
        token_prefix=token_prefix,
        principal_id="scientist-ada",
        tenant_id="tenant-oncology",
        scopes=frozenset({Scope.INFERENCE_INVOKE.value}),
        models=frozenset({"rfdiffusion"}),
        max_concurrency=1,
    )
    operation = await postgres_admin_store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="rfdiffusion",
            operation="generate-backbone",
            protocol="scientific-batch-v1",
            idempotency_key="scientific-admin-postgres-0001",
            request_body=b'{"schema":"fs2-serve.nebius.ai/scientific-run-request/v1"}',
        ),
        model_revision="2" * 40,
        reserved_gpu_seconds=0,
        max_attempts=1,
    )
    input_attempt_id = uuid4()
    input_artifact_id = uuid4()
    async with postgres_admin_store.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_scientific_stage_attempts(
                attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status,
                started_at,retention_expires_at
            ) VALUES($1,$2,$3,'input','-',1,'running',$4::timestamptz,
                $4::timestamptz + interval '1 day')
            """,
            input_attempt_id,
            operation.id,
            principal.tenant_id,
            operation.accepted_at,
        )
        await connection.execute(
            """
            INSERT INTO fs2_scientific_artifacts(
                id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,
                size_bytes,media_type,storage_key,access_profile,retention_expires_at
            ) VALUES($1,$2,$3,$4,'input','-','input',$5,128,'application/json',$6,'public',
                $7::timestamptz + interval '1 day')
            """,
            input_artifact_id,
            input_attempt_id,
            operation.id,
            principal.tenant_id,
            _digest("admin-input"),
            f"scientific/v1/tenants/{principal.tenant_id}/operations/{operation.id}/"
            f"stages/input/shards/-/attempts/{input_attempt_id}/input/sha256/"
            f"{_digest('admin-input').removeprefix('sha256:')}",
            operation.accepted_at,
        )
    proposed = _state(
        operation.id,
        captured_at=operation.accepted_at,
        input_artifact_id=input_artifact_id,
    )
    batches = PostgresScientificBatchRepository(postgres_admin_store.pool)
    await batches.create(
        operation_id=operation.id,
        tenant_id=principal.tenant_id,
        model_id="rfdiffusion",
        variant_id=proposed.variant_id,
        input_artifact_id=proposed.input_artifact_id,
        plan=proposed.plan,
        scheduling=proposed.scheduling,
    )
    adapter = PostgresScientificRunAdminAdapter(
        pool=postgres_admin_store.pool,
        batches=batches,
        models=ModelAdapter(),
    )

    snapshot = await adapter.list_runs(
        ScientificRunQuery(
            from_at=operation.accepted_at - timedelta(minutes=1),
            to_at=operation.accepted_at + timedelta(minutes=1),
            tenant_id="tenant-oncology",
        )
    )

    assert len(snapshot.data.items) == 1
    item = snapshot.data.items[0]
    assert item.id == str(operation.id)
    assert item.attribution.user_id == "researcher-ada"
    assert item.attribution.api_key_prefix == token_prefix
    assert item.queue.cluster_queue == "inference-accelerators"
    assert item.gpu_accounting.gpu_count is None
    assert item.gpu_accounting.active.evidence.value == "unavailable"

    document = json.loads(
        (
            Path(__file__).parents[3] / "catalog/runtime/contracts/examples/scientific-run-result.example.json"
        ).read_text()
    )
    document.update(
        {
            "operation_id": str(operation.id),
            "batch_id": str(proposed.batch_id),
            "workload_id": str(proposed.workload_id),
        }
    )
    result = ScientificRunResult.model_validate(document)
    artifact_repository = PostgresArtifactRepository(postgres_admin_store.pool)
    await artifact_repository.commit_run_result(
        RunResultRecord(
            operation_id=operation.id,
            tenant_id=principal.tenant_id,
            result=result,
            result_digest=result.digest,
            committed_at=NOW,
            retention_expires_at=NOW + timedelta(days=90),
        )
    )
    artifact_service = ScientificArtifactService(
        repository=artifact_repository,
        object_store=cast(Any, object()),
        allowed_media_types={"application/vnd.fs2.scientific-manifest+json"},
    )
    artifact_snapshot = await PostgresScientificArtifactAdminAdapter(artifact_service).for_operation(
        operation.id,
        tenant_id=principal.tenant_id,
    )
    assert artifact_snapshot.terminal_status == "succeeded"
    assert artifact_snapshot.attempts[0].gpu_count == 1
    assert artifact_snapshot.semantic_validation.status == "passed"
