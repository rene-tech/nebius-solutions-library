from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.scientific_batch import (
    AttemptArtifactCommit,
    AttemptOutcome,
    BatchStatus,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    SchedulingSnapshot,
    ScientificBatchController,
    ScientificBatchPlan,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
    StageStatus,
    WorkloadKind,
    WorkloadObservation,
    WorkloadState,
)
from fs2_serve.scientific_batch.models import BatchEventKind
from fs2_serve.scientific_batch.protocols import BatchRepositoryConflictError

NOW = datetime(2026, 9, 2, 20, 40, tzinfo=UTC)
PHASES = (
    LifecyclePhase.ADMITTED,
    LifecyclePhase.NODE_PENDING,
    LifecyclePhase.IMAGE_LOADING,
    LifecyclePhase.ARTIFACT_LOADING,
    LifecyclePhase.RESTORING,
    LifecyclePhase.SEMANTIC_WARMUP,
    LifecyclePhase.ACTIVE_COMPUTE,
    LifecyclePhase.ALLOCATED_IDLE,
    LifecyclePhase.GRACE_DRAIN,
    LifecyclePhase.TEARDOWN,
)


def digest(seed: str) -> str:
    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


def plan() -> ScientificBatchPlan:
    return ScientificBatchPlan(
        stages=(
            ScientificStagePlan(stage_id="prepare", shards=("target-a", "target-b"), max_attempts=2),
            ScientificStagePlan(
                stage_id="design",
                depends_on=("prepare",),
                mode=ExecutionMode.TRUE_GANG,
                shards=("gang",),
                gang_size=4,
                max_attempts=2,
            ),
        )
    )


def snapshot(
    batch_plan: ScientificBatchPlan,
    *,
    service_class: ServiceClass = ServiceClass.CUSTOMER_BATCH,
) -> SchedulingSnapshot:
    return SchedulingSnapshot(
        policy_revision=digest("admission-policy-v1"),
        captured_at=NOW,
        service_class=service_class,
        tenant_queue="cancer-immunotherapy",
        model_lane="protein-design",
        stages=tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific-batch",
                workload_priority_class=f"fs2-{service_class}",
                workload_priority_value=100,
                resolved_pool_preference=("h100-preemptible", "h100-capacity-block"),
                admitted_resource_flavor="inference-h100-1x",
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=stage.gang_size or 1,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=stage.checkpoint_mode,
                preemption_mode=stage.preemption_mode,
            )
            for stage in batch_plan.stages
        ),
    )


def controller(repository, cluster) -> ScientificBatchController:
    return ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-pod:uid-1",
        namespace="fs2-scientific",
        clock=lambda: NOW,
    )


def commit(operation_id: UUID, stage_id: str, attempt_id: UUID, *, valid: bool = True):
    handoff_id, manifest_id, validation_id = uuid4(), uuid4(), uuid4()
    return AttemptArtifactCommit(
        operation_id=operation_id,
        stage_id=stage_id,
        attempt_ids=(attempt_id,),
        logical_artifact_id=f"{stage_id}-result-{str(attempt_id)[:8]}",
        handoff_artifact_id=handoff_id,
        handoff_digest=digest(f"handoff-{stage_id}-{attempt_id}"),
        handoff_size_bytes=100,
        handoff_media_type="application/json",
        handoff_compression=None,
        manifest_artifact_id=manifest_id,
        validation_artifact_id=validation_id,
        manifest_digest=digest(f"manifest-{stage_id}"),
        validation_digest=digest(f"validation-{stage_id}"),
        committed_at=NOW,
        validated_at=NOW,
        semantic_valid=valid,
    )


def observe_success(cluster: FakeScientificBatchCluster, attempt) -> None:
    cluster.set_observation(
        attempt.workload,
        WorkloadObservation(
            ref=attempt.workload,
            attempt_id=attempt.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=PHASES,
        ),
    )


@pytest.mark.asyncio
async def test_frozen_snapshot_validated_commit_gates_next_stage_and_releases_fanout() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = plan()
    frozen = snapshot(batch_plan)

    admitted = await reconciler.admit(
        operation_id=operation_id,
        tenant_id="cancer-immunotherapy",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=frozen,
    )
    assert admitted.operation_id == operation_id
    assert admitted.batch_id != admitted.operation_id
    assert admitted.workload_id not in {admitted.operation_id, admitted.batch_id}
    assert admitted.scheduling.digest == frozen.digest
    with pytest.raises(BatchRepositoryConflictError):
        await reconciler.admit(
            operation_id=operation_id,
            tenant_id="cancer-immunotherapy",
            model_id="protein-design",
            plan=batch_plan,
            scheduling=snapshot(batch_plan, service_class=ServiceClass.INTERACTIVE),
        )

    await reconciler.reconcile_once()
    running = repository.records[operation_id]
    prepare = running.stage("prepare")
    assert prepare.status is StageStatus.ACTIVE
    assert [resource.kind for resource in cluster.apply_history] == [WorkloadKind.JOB, WorkloadKind.JOB]
    assert all(
        resource.scheduling.workload_priority_class == "fs2-customer-batch" for resource in cluster.apply_history
    )
    assert not any(resource.kind is WorkloadKind.JOB_SET for resource in cluster.apply_history)

    for attempt in prepare.attempts:
        observe_success(cluster, attempt)
    await reconciler.reconcile_once()

    awaiting_commit = repository.records[operation_id]
    assert awaiting_commit.stage("prepare").status is StageStatus.ACTIVE
    assert awaiting_commit.stage("design").status is StageStatus.PENDING
    assert len(cluster.delete_history) == 0
    assert all(
        attempt.outcome is AttemptOutcome.SUCCEEDED and not attempt.resource_released
        for attempt in awaiting_commit.stage("prepare").attempts
    )

    await reconciler.reconcile_once()
    awaiting_commit = repository.records[operation_id]
    assert len(cluster.delete_history) == 2
    assert all(attempt.resource_released for attempt in awaiting_commit.stage("prepare").attempts)
    waiting_revision = awaiting_commit.revision
    await reconciler.reconcile_once()
    assert repository.records[operation_id].revision == waiting_revision
    assert not any(resource.kind is WorkloadKind.JOB_SET for resource in cluster.apply_history)

    for attempt in prepare.attempts:
        repository.put_commit(commit(operation_id, "prepare", attempt.attempt_id))
    await reconciler.reconcile_once()
    prepared = repository.records[operation_id]
    assert prepared.stage("prepare").status is StageStatus.SUCCEEDED
    assert prepared.stage("design").status is StageStatus.PENDING
    assert len(cluster.delete_history) == 2
    assert not any(resource.kind is WorkloadKind.JOB_SET for resource in cluster.apply_history)

    await reconciler.reconcile_once()
    design = repository.records[operation_id].stage("design")
    assert design.status is StageStatus.ACTIVE
    assert len(design.attempts) == 1
    assert cluster.apply_history[-1].kind is WorkloadKind.JOB_SET
    assert cluster.apply_history[-1].gang_size == 4
    assert len(cluster.delete_history) == 2, "the downstream JobSet starts only after predecessor quota release"

    observe_success(cluster, design.attempts[0])
    repository.put_commit(commit(operation_id, "design", design.attempts[0].attempt_id))
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    final = repository.records[operation_id]
    assert final.status is BatchStatus.SUCCEEDED

    events = repository.events[operation_id]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    first_attempt_phases = [
        event.draft.phase
        for event in events
        if event.draft.kind is BatchEventKind.LIFECYCLE and event.draft.attempt_id == prepare.attempts[0].attempt_id
    ]
    assert first_attempt_phases == [LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING, *PHASES]
    assert len({event.draft.event_id for event in events}) == len(events)


@pytest.mark.asyncio
async def test_terminal_result_publication_is_durable_and_retried_idempotently() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()

    class Publisher:
        def __init__(self) -> None:
            self.calls = 0

        async def publish_terminal(self, state) -> None:
            assert state.status is BatchStatus.SUCCEEDED
            assert state.result_published is False
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("injected artifact result failure")

    publisher = Publisher()
    reconciler = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-pod:uid-1",
        namespace="fs2-scientific",
        result_publisher=publisher,
        clock=lambda: NOW,
    )
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="fold"),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    attempt = repository.records[operation_id].stage("fold").attempts[0]
    observe_success(cluster, attempt)
    repository.put_commit(commit(operation_id, "fold", attempt.attempt_id))
    for _ in range(3):
        await reconciler.reconcile_once()
    terminal = repository.records[operation_id]
    assert terminal.status is BatchStatus.SUCCEEDED
    assert terminal.result_published is False

    with pytest.raises(RuntimeError, match="injected artifact result failure"):
        await reconciler.reconcile_once()
    assert repository.records[operation_id].result_published is False
    await reconciler.reconcile_once()
    assert repository.records[operation_id].result_published is True
    assert publisher.calls == 2


@pytest.mark.asyncio
async def test_attempt_identity_is_durable_before_apply_and_recovered_after_crash() -> None:
    repository = FakeScientificBatchRepository()

    class FailFirstApplyCluster(FakeScientificBatchCluster):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def apply(self, resource, *, controller_fence: int):
            reserved = repository.records[resource.operation_id].stage(resource.stage_id)
            attempt = reserved.latest_attempt(resource.shard_id)
            assert attempt is not None
            assert attempt.attempt_id == resource.attempt_id
            assert attempt.workload.uid is None
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected apply crash")
            return await super().apply(resource, controller_fence=controller_fence)

    cluster = FailFirstApplyCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="fold"),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )

    with pytest.raises(RuntimeError, match="injected apply crash"):
        await reconciler.reconcile_once()
    reserved = repository.records[operation_id].stage("fold").attempts[0]
    assert reserved.workload.uid is None

    await reconciler.reconcile_once()
    recovered = repository.records[operation_id].stage("fold").attempts[0]
    assert recovered.attempt_id == reserved.attempt_id
    assert recovered.workload.uid is not None
    assert len(cluster.apply_history) == 1


@pytest.mark.asyncio
async def test_preemption_retries_with_new_attempt_and_stale_observation_is_fenced() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="fold", max_attempts=2),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    first = repository.records[operation_id].stage("fold").attempts[0]
    cluster.set_observation(
        first.workload,
        WorkloadObservation(
            ref=first.workload,
            attempt_id=first.attempt_id,
            state=WorkloadState.PREEMPTED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
            failure_kind=FailureKind.PREEMPTION,
            failure_code="node_preempted",
        ),
    )
    await reconciler.reconcile_once()
    observed = repository.records[operation_id].stage("fold").latest_attempt("main")
    assert observed is not None and observed.outcome is AttemptOutcome.PREEMPTED
    assert not observed.resource_released
    await reconciler.reconcile_once()
    assert repository.records[operation_id].stage("fold").status is StageStatus.PENDING

    await reconciler.reconcile_once()
    stage = repository.records[operation_id].stage("fold")
    second = stage.latest_attempt("main")
    assert second is not None
    assert second.attempt_number == 2 and second.attempt_id != first.attempt_id
    assert second.workload.name != first.workload.name
    assert all(
        resource.workload_id == repository.records[operation_id].workload_id for resource in cluster.apply_history
    )

    cluster.set_observation(
        second.workload,
        WorkloadObservation(
            ref=second.workload,
            attempt_id=first.attempt_id,
            state=WorkloadState.SUCCEEDED,
            phases=PHASES,
        ),
    )
    await reconciler.reconcile_once()
    still_active = repository.records[operation_id].stage("fold").latest_attempt("main")
    assert still_active is not None and still_active.outcome is AttemptOutcome.ACTIVE
    assert repository.events[operation_id][-1].draft.kind is BatchEventKind.ATTEMPT_FENCED

    observe_success(cluster, second)
    repository.put_commit(commit(operation_id, "fold", second.attempt_id))
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    assert repository.records[operation_id].status is BatchStatus.SUCCEEDED
    assert len(cluster.apply_history) == 2
    assert any(event.draft.kind is BatchEventKind.RETRY_SCHEDULED for event in repository.events[operation_id])


@pytest.mark.asyncio
async def test_frozen_queue_deadline_fails_without_retry_before_admission() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    timestamps = iter((NOW, NOW + timedelta(seconds=601), NOW + timedelta(seconds=602)))
    reconciler = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller-pod:uid-1",
        namespace="fs2-scientific",
        clock=lambda: next(timestamps),
    )
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="fold", max_attempts=3),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )

    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    expired = repository.records[operation_id].stage("fold").attempts[0]
    assert expired.outcome is AttemptOutcome.FAILED
    assert expired.failure_kind is FailureKind.APPLICATION
    assert expired.failure_code == "queue_deadline_exceeded"

    await reconciler.reconcile_once()
    assert repository.records[operation_id].status is BatchStatus.FAILED
    assert len(cluster.apply_history) == 1
    assert cluster.delete_history == [expired.workload]


@pytest.mark.asyncio
async def test_infrastructure_failure_is_retried_but_phase_replay_is_idempotent() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="dock", max_attempts=2),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    first = repository.records[operation_id].stage("dock").attempts[0]
    running = WorkloadObservation(
        ref=first.workload,
        attempt_id=first.attempt_id,
        state=WorkloadState.RUNNING,
        phases=(LifecyclePhase.ADMITTED, LifecyclePhase.NODE_PENDING, LifecyclePhase.IMAGE_LOADING),
    )
    cluster.set_observation(first.workload, running)
    await reconciler.reconcile_once()
    event_count = len(repository.events[operation_id])
    revision = repository.records[operation_id].revision
    await reconciler.reconcile_once()
    assert len(repository.events[operation_id]) == event_count
    assert repository.records[operation_id].revision == revision

    cluster.set_observation(
        first.workload,
        WorkloadObservation(
            ref=first.workload,
            attempt_id=first.attempt_id,
            state=WorkloadState.FAILED,
            phases=running.phases,
            failure_kind=FailureKind.INFRASTRUCTURE,
            failure_code="node_lost",
        ),
    )
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    latest = repository.records[operation_id].stage("dock").latest_attempt("main")
    assert latest is not None and latest.attempt_number == 2
    assert len(cluster.apply_history) == 2


@pytest.mark.asyncio
async def test_terminal_observation_is_durable_before_idempotent_workload_cleanup() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="dock"),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    attempt = repository.records[operation_id].stage("dock").attempts[0]
    observe_success(cluster, attempt)

    await reconciler.reconcile_once()
    observed = repository.records[operation_id].stage("dock").attempts[0]
    assert observed.outcome is AttemptOutcome.SUCCEEDED
    assert not observed.resource_released
    assert cluster.delete_calls == []

    repository.fail_next_replace = True
    with pytest.raises(RuntimeError, match="injected durable replace failure"):
        await reconciler.reconcile_once()
    assert not repository.records[operation_id].stage("dock").attempts[0].resource_released
    assert cluster.delete_calls == [attempt.workload]

    await reconciler.reconcile_once()
    cleaned = repository.records[operation_id].stage("dock").attempts[0]
    assert cleaned.resource_released
    assert cluster.delete_calls == [attempt.workload, attempt.workload]


@pytest.mark.asyncio
async def test_cancel_racing_with_job_creation_is_carried_into_fenced_state() -> None:
    repository = FakeScientificBatchRepository()

    class CancellingCluster(FakeScientificBatchCluster):
        async def apply(self, resource, *, controller_fence: int):
            ref = await super().apply(resource, controller_fence=controller_fence)
            await repository.request_cancel(
                resource.operation_id,
                tenant_id=resource.tenant_id,
                actor="scientist-a",
            )
            return ref

    cluster = CancellingCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="fold"),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )

    await reconciler.reconcile_once()
    recorded = repository.records[operation_id]
    assert recorded.cancel_requested is True
    assert recorded.stage("fold").status is StageStatus.ACTIVE
    assert len(cluster.apply_history) == 1

    await reconciler.reconcile_once()
    cancelled = repository.records[operation_id]
    assert cancelled.status is BatchStatus.CANCELLED
    assert cancelled.stage("fold").attempts[0].outcome is AttemptOutcome.CANCELLED
    assert cluster.delete_history == [cancelled.stage("fold").attempts[0].workload]


@pytest.mark.asyncio
async def test_negative_semantic_commit_cannot_unlock_downstream_stage() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(
        stages=(
            ScientificStagePlan(stage_id="prepare"),
            ScientificStagePlan(stage_id="infer", depends_on=("prepare",)),
        )
    )
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    attempt = repository.records[operation_id].stage("prepare").attempts[0]
    observe_success(cluster, attempt)
    repository.put_commit(commit(operation_id, "prepare", attempt.attempt_id, valid=False))
    await reconciler.reconcile_once()
    observed = repository.records[operation_id].stage("prepare").attempts[0]
    assert observed.outcome is AttemptOutcome.SUCCEEDED and not observed.resource_released
    await reconciler.reconcile_once()

    failed = repository.records[operation_id]
    assert failed.status is BatchStatus.FAILED
    assert failed.stage("prepare").failure_code == "artifact_commit_fenced_or_invalid"
    assert failed.stage("infer").status is StageStatus.CANCELLED
    assert len(cluster.apply_history) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "failure_code"),
    [
        (FailureKind.USER_INPUT, "invalid_sequence"),
        (FailureKind.SCIENTIFIC_VALIDATION, "invalid_structure"),
        (FailureKind.APPLICATION, "model_rejected_request"),
    ],
)
async def test_only_infrastructure_failures_are_retried(failure_kind: FailureKind, failure_code: str) -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="score", max_attempts=3),))
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    attempt = repository.records[operation_id].stage("score").attempts[0]
    cluster.set_observation(
        attempt.workload,
        WorkloadObservation(
            ref=attempt.workload,
            attempt_id=attempt.attempt_id,
            state=WorkloadState.FAILED,
            phases=(LifecyclePhase.ADMITTED, LifecyclePhase.ACTIVE_COMPUTE),
            failure_kind=failure_kind,
            failure_code=failure_code,
        ),
    )
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    failed = repository.records[operation_id]
    assert failed.status is BatchStatus.FAILED
    assert failed.failure_code == failure_code
    assert len(cluster.apply_history) == 1
    assert not any(event.draft.kind is BatchEventKind.RETRY_SCHEDULED for event in repository.events[operation_id])


@pytest.mark.asyncio
async def test_cancel_cascades_to_active_attempts_and_never_creates_downstream_stage() -> None:
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    reconciler = controller(repository, cluster)
    operation_id = uuid4()
    batch_plan = plan()
    await reconciler.admit(
        operation_id=operation_id,
        tenant_id="tenant-a",
        model_id="protein-design",
        plan=batch_plan,
        scheduling=snapshot(batch_plan),
    )
    await reconciler.reconcile_once()
    repository.force_cancel(operation_id)
    await reconciler.reconcile_once()

    cancelled = repository.records[operation_id]
    assert cancelled.status is BatchStatus.CANCELLED
    assert [stage.status for stage in cancelled.stages] == [StageStatus.CANCELLED, StageStatus.CANCELLED]
    assert all(attempt.outcome is AttemptOutcome.CANCELLED for attempt in cancelled.stage("prepare").attempts)
    assert all(attempt.resource_released for attempt in cancelled.stage("prepare").attempts)
    assert len(cluster.delete_history) == 2
    assert not any(resource.kind is WorkloadKind.JOB_SET for resource in cluster.apply_history)
    phases = [event.draft.phase for event in repository.events[operation_id] if event.draft.phase is not None]
    assert LifecyclePhase.GRACE_DRAIN in phases and LifecyclePhase.TEARDOWN in phases
    assert repository.events[operation_id][-1].draft.kind is BatchEventKind.BATCH_CANCELLED


def test_invalid_dag_and_snapshot_are_rejected() -> None:
    with pytest.raises(ValueError, match="topological order"):
        ScientificBatchPlan(
            stages=(
                ScientificStagePlan(stage_id="one", depends_on=("two",)),
                ScientificStagePlan(stage_id="two", depends_on=("one",)),
            )
        )
    base = plan()
    frozen = snapshot(base)
    with pytest.raises(ValueError, match="unique"):
        replace(frozen, stages=(frozen.stages[0], frozen.stages[0]))
