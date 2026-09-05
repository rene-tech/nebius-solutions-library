from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from fs2_serve.lifecycle import (
    LifecycleClock,
    LifecycleEdge,
    MemoryLifecycleRepository,
)
from fs2_serve.lifecycle import (
    LifecyclePhase as LedgerPhase,
)
from fs2_serve.models import OperationStatus, OperationView
from fs2_serve.scientific_batch.kubernetes import ScientificKubernetesError, _pod_lifecycle
from fs2_serve.scientific_batch.lifecycle_bridge import ScientificLifecycleBridge
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    BatchEvent,
    BatchEventDraft,
    BatchEventKind,
    BatchStatus,
    CheckpointMode,
    FailureKind,
    LifecyclePhase,
    PodLifecycleObservation,
    PodPhaseInterval,
    PreemptionMode,
    ResourceClass,
    SchedulingAdmission,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ScientificStageState,
    ServiceClass,
    StageSchedulingDecision,
    StageStatus,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadState,
)

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
GPU_UUID = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class EventSource:
    def __init__(self, events: list[BatchEvent]) -> None:
        self.events = events

    async def list_events(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[BatchEvent]:
        assert operation_id == self.events[0].draft.operation_id
        assert tenant_id == "oncology-a"
        return [value for value in self.events if value.sequence > after_sequence][:limit]


class OperationSource:
    def __init__(self, operation: OperationView) -> None:
        self.operation = operation

    async def get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> OperationView:
        assert operation_id == self.operation.id
        assert tenant_id in {None, self.operation.tenant_id}
        return self.operation


def operation(
    operation_id: UUID,
    *,
    status: OperationStatus,
    traceparent: str | None = None,
) -> OperationView:
    return OperationView(
        id=operation_id,
        tenant_id="oncology-a",
        principal_id="scientist@example.test",
        token_id=uuid4(),
        model_id="rfdiffusion",
        model_revision=digest("rfdiffusion-runtime"),
        protocol="scientific-batch-v1",
        operation="design",
        idempotency_key="scientific-lifecycle-test",
        traceparent=traceparent,
        status=status,
        accepted_at=NOW,
        available_at=NOW,
    )


def terminal_state(outcome: AttemptOutcome) -> tuple[ScientificBatchState, ScientificAttemptState]:
    plan = ScientificBatchPlan(
        stages=(
            ScientificStagePlan(
                stage_id="design",
                shards=("target",),
                max_attempts=1,
                checkpoint_mode=CheckpointMode.RESUME,
                preemption_mode=PreemptionMode.CHECKPOINTABLE,
            ),
        )
    )
    scheduling = SchedulingSnapshot(
        policy_revision=digest("policy"),
        captured_at=NOW,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="oncology-a",
        model_lane="protein-design",
        workload_namespace="fs2-scientific",
        route_namespace="fs2-scientific",
        stages=(
            StageSchedulingDecision(
                stage_id="design",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific-batch",
                workload_priority_class="fs2-customer-batch",
                workload_priority_value=100,
                resolved_pool_preference=("h100-preemptible",),
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=CheckpointMode.RESUME,
                preemption_mode=PreemptionMode.CHECKPOINTABLE,
            ),
        ),
    )
    admitted = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="oncology-a",
        model_id="rfdiffusion",
        variant_id="base",
        input_artifact_id=uuid4(),
        plan=plan,
        scheduling=scheduling,
    )
    attempt = ScientificAttemptState(
        attempt_id=uuid4(),
        stage_id="design",
        shard_id="target",
        attempt_number=1,
        workload=WorkloadRef(
            namespace="fs2-scientific",
            name="fs2-design-target-a1",
            kind=WorkloadKind.JOB,
            uid="job-uid-1",
        ),
        started_at=NOW,
        completed_at=at(12),
        outcome=outcome,
        last_phase=LifecyclePhase.TEARDOWN,
        deletion_requested=True,
        resource_released=True,
        scheduling_admission=SchedulingAdmission(
            resolved_pool_id="h100-preemptible",
            admitted_resource_flavor="inference-h100-1x",
            accelerator_resource_name="nvidia.com/gpu",
            accelerator_count=1,
            quota_reserved_at=at(1),
            admitted_at=at(2),
        ),
        kueue_workload_uid="kueue-uid-1",
        pod_uids=("pod-uid-1",),
        failure_kind=(
            FailureKind.PREEMPTION
            if outcome is AttemptOutcome.PREEMPTED
            else FailureKind.APPLICATION
            if outcome is AttemptOutcome.FAILED
            else None
        ),
        failure_code="Preempted" if outcome is AttemptOutcome.PREEMPTED else None,
    )
    batch_status = {
        AttemptOutcome.SUCCEEDED: BatchStatus.SUCCEEDED,
        AttemptOutcome.CANCELLED: BatchStatus.CANCELLED,
        AttemptOutcome.FAILED: BatchStatus.FAILED,
        AttemptOutcome.PREEMPTED: BatchStatus.FAILED,
    }[outcome]
    stage_status = {
        AttemptOutcome.SUCCEEDED: StageStatus.SUCCEEDED,
        AttemptOutcome.CANCELLED: StageStatus.CANCELLED,
        AttemptOutcome.FAILED: StageStatus.FAILED,
        AttemptOutcome.PREEMPTED: StageStatus.FAILED,
    }[outcome]
    state = replace(
        admitted,
        stages=(ScientificStageState(stage_id="design", status=stage_status, attempts=(attempt,)),),
        status=batch_status,
        revision=9,
        cancel_requested=outcome is AttemptOutcome.CANCELLED,
        failure_code="Preempted" if outcome is AttemptOutcome.PREEMPTED else None,
    )
    return state, attempt


def lifecycle_events(
    state: ScientificBatchState,
    attempt: ScientificAttemptState,
    *,
    preempted: bool,
) -> list[BatchEvent]:
    timeline = [
        (LifecyclePhase.QUEUED, 0),
        (LifecyclePhase.ADMITTED, 2),
        (LifecyclePhase.IMAGE_LOADING, 2),
        (LifecyclePhase.ARTIFACT_LOADING, 3),
        (LifecyclePhase.RESTORING, 4),
        (LifecyclePhase.SEMANTIC_WARMUP, 5),
        (LifecyclePhase.ACTIVE_COMPUTE, 6),
        (LifecyclePhase.ALLOCATED_IDLE, 9),
    ]
    if preempted:
        timeline.append((LifecyclePhase.PREEMPTED, 10))
    timeline.extend(((LifecyclePhase.GRACE_DRAIN, 10), (LifecyclePhase.TEARDOWN, 12)))
    return [
        BatchEvent(
            sequence=sequence,
            occurred_at=at(offset),
            draft=BatchEventDraft.build(
                operation_id=state.operation_id,
                batch_id=state.batch_id,
                workload_id=state.workload_id,
                kind=BatchEventKind.LIFECYCLE,
                stage_id=attempt.stage_id,
                shard_id=attempt.shard_id,
                attempt_id=attempt.attempt_id,
                phase=phase,
            ),
        )
        for sequence, (phase, offset) in enumerate(timeline, start=1)
    ]


def pod_observation(
    attempt: ScientificAttemptState,
    *,
    observed_at: datetime,
) -> WorkloadObservation:
    pod = PodLifecycleObservation(
        pod_uid="pod-uid-1",
        pod_name="fs2-design-target-a1-abcde",
        node_name="h100-node-1",
        node_uid="node-uid-1",
        created_at=NOW,
        observed_at=observed_at,
        scheduled_at=at(2),
        gpu_count=1,
        gpu_uuids=(GPU_UUID,),
        device_allocation_observed_at=at(3),
        device_observation_resolution_seconds=5,
        completed_at=at(10),
        phases=(
            PodPhaseInterval(LifecyclePhase.IMAGE_LOADING, at(2), at(3)),
            PodPhaseInterval(LifecyclePhase.ARTIFACT_LOADING, at(3), at(4)),
            PodPhaseInterval(LifecyclePhase.RESTORING, at(4), at(5)),
            PodPhaseInterval(LifecyclePhase.SEMANTIC_WARMUP, at(5), at(6)),
            PodPhaseInterval(LifecyclePhase.ACTIVE_COMPUTE, at(6), at(9)),
            PodPhaseInterval(LifecyclePhase.ALLOCATED_IDLE, at(9), at(10)),
        ),
    )
    return WorkloadObservation(
        ref=attempt.workload,
        attempt_id=attempt.attempt_id,
        state=(
            WorkloadState.PREEMPTED
            if attempt.outcome is AttemptOutcome.PREEMPTED
            else WorkloadState.FAILED
            if attempt.outcome is AttemptOutcome.CANCELLED
            else WorkloadState.SUCCEEDED
        ),
        phases=tuple(value.phase for value in pod.phases),
        scheduling_admission=attempt.scheduling_admission,
        kueue_workload_uid=attempt.kueue_workload_uid,
        pod_uids=(pod.pod_uid,),
        pod_lifecycle=(pod,),
        failure_kind=(
            FailureKind.PREEMPTION
            if attempt.outcome is AttemptOutcome.PREEMPTED
            else FailureKind.APPLICATION
            if attempt.outcome is AttemptOutcome.CANCELLED
            else None
        ),
        failure_code=(
            "Preempted"
            if attempt.outcome is AttemptOutcome.PREEMPTED
            else "cancelled"
            if attempt.outcome is AttemptOutcome.CANCELLED
            else None
        ),
    )


def raw_pod(*, init_finished: bool) -> dict[str, object]:
    restore_state: dict[str, object]
    stage_state: dict[str, object]
    if init_finished:
        restore_state = {
            "terminated": {
                "startedAt": at(3).isoformat(),
                "finishedAt": at(4).isoformat(),
                "exitCode": 0,
            }
        }
        stage_state = {"running": {"startedAt": at(5).isoformat()}}
    else:
        restore_state = {"running": {"startedAt": at(3).isoformat()}}
        stage_state = {"waiting": {"reason": "PodInitializing"}}
    return {
        "metadata": {
            "uid": "pod-uid-1",
            "name": "fs2-design-target-a1-abcde",
            "creationTimestamp": NOW.isoformat(),
            "annotations": {
                "telemetry.fs2.nebius.ai/gpu-uuids": f'["{GPU_UUID}"]',
                "telemetry.fs2.nebius.ai/gpu-allocation-observed-at": at(2).isoformat(),
                "telemetry.fs2.nebius.ai/gpu-observer-resolution-seconds": "5",
            },
        },
        "spec": {
            "nodeName": "h100-node-1",
            "initContainers": [{"name": "prepare-workspace"}, {"name": "restore-checkpoint"}],
            "containers": [
                {
                    "name": "scientific-stage",
                    "resources": {"requests": {"nvidia.com/gpu": "1"}},
                },
                {"name": "artifact-collector"},
            ],
        },
        "status": {
            "phase": "Running",
            "startTime": NOW.isoformat(),
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True",
                    "lastTransitionTime": NOW.isoformat(),
                }
            ],
            "initContainerStatuses": [
                {
                    "name": "prepare-workspace",
                    "state": {
                        "terminated": {
                            "startedAt": at(1).isoformat(),
                            "finishedAt": at(2).isoformat(),
                            "exitCode": 0,
                        }
                    },
                },
                {"name": "restore-checkpoint", "state": restore_state},
            ],
            "containerStatuses": [
                {"name": "scientific-stage", "state": stage_state},
                {"name": "artifact-collector", "state": {"running": {"startedAt": at(5).isoformat()}}},
            ],
        },
    }


def test_running_pod_is_not_active_compute_until_every_init_container_succeeds() -> None:
    initializing = _pod_lifecycle(
        raw_pod(init_finished=False),
        accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(4),
    )
    active = _pod_lifecycle(
        raw_pod(init_finished=True),
        accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(6),
    )

    assert initializing is not None and active is not None
    assert initializing.created_at == NOW
    assert active.created_at == NOW
    assert LifecyclePhase.RESTORING in {value.phase for value in initializing.phases}
    assert LifecyclePhase.ACTIVE_COMPUTE not in {value.phase for value in initializing.phases}
    assert LifecyclePhase.ACTIVE_COMPUTE in {value.phase for value in active.phases}
    assert next(value for value in active.phases if value.phase is LifecyclePhase.ACTIVE_COMPUTE).started_at == at(5)
    assert (
        PodPhaseInterval(LifecyclePhase.IMAGE_LOADING, at(2), at(3)) in active.phases
        and PodPhaseInterval(LifecyclePhase.IMAGE_LOADING, at(4), at(5)) in active.phases
    )


def test_running_init_does_not_synthesize_future_init_phase_evidence() -> None:
    baseline_raw = raw_pod(init_finished=False)
    future_raw = raw_pod(init_finished=False)
    init_specs = future_raw["spec"]["initContainers"]
    assert isinstance(init_specs, list)
    init_specs.append({"name": "prepare-future-input"})

    baseline = _pod_lifecycle(
        baseline_raw,
        accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(4),
    )
    with_future_init = _pod_lifecycle(
        future_raw,
        accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(4),
    )

    assert baseline is not None and with_future_init is not None
    assert with_future_init == baseline
    identities = [(value.phase, value.started_at) for value in with_future_init.phases]
    assert len(identities) == len(set(identities))


def test_terminal_pod_retains_device_observation_and_container_completion() -> None:
    raw = raw_pod(init_finished=True)
    status = raw["status"]
    assert isinstance(status, dict)
    status["phase"] = "Succeeded"
    status["containerStatuses"] = [
        {
            "name": "scientific-stage",
            "state": {
                "terminated": {
                    "startedAt": at(5).isoformat(),
                    "finishedAt": at(9).isoformat(),
                    "exitCode": 0,
                }
            },
        },
        {
            "name": "artifact-collector",
            "state": {
                "terminated": {
                    "startedAt": at(5).isoformat(),
                    "finishedAt": at(10).isoformat(),
                    "exitCode": 0,
                }
            },
        },
    ]

    observed = _pod_lifecycle(
        raw,
        accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(11),
    )

    assert observed is not None
    assert observed.device_allocation_observed_at == at(2)
    assert observed.device_observation_resolution_seconds == 5
    assert observed.completed_at == at(10)


def test_partial_gpu_observer_annotations_fail_closed() -> None:
    raw = raw_pod(init_finished=True)
    metadata = raw["metadata"]
    assert isinstance(metadata, dict)
    annotations = metadata["annotations"]
    assert isinstance(annotations, dict)
    del annotations["telemetry.fs2.nebius.ai/gpu-allocation-observed-at"]

    with pytest.raises(ScientificKubernetesError, match="annotations are incomplete"):
        _pod_lifecycle(
            raw,
            accelerator_resource_name="nvidia.com/gpu",
            observed_at=at(6),
        )


@pytest.mark.asyncio
async def test_subject_retains_operation_trace_context_without_exposing_traceparent() -> None:
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    traceparent = "00-11111111111111111111111111111111-2222222222222222-01"
    operation_view = operation(
        state.operation_id,
        status=OperationStatus.SUCCEEDED,
        traceparent=traceparent,
    )
    repository = MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(operation_view),
    )

    await bridge.sync(state)

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None
    assert detail.subject.trace_id == "1" * 32
    assert detail.subject.parent_span_id == "2" * 16
    assert "traceparent" not in operation_view.model_dump(mode="json")


@pytest.mark.asyncio
async def test_terminal_cleanup_gap_is_teardown_and_never_active_compute() -> None:
    state, original_attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    attempt = replace(original_attempt, completed_at=at(10))
    state = replace(
        state,
        stages=(ScientificStageState(stage_id="design", status=StageStatus.SUCCEEDED, attempts=(attempt,)),),
    )
    events = [
        event
        for event in lifecycle_events(state, attempt, preempted=False)
        if event.draft.phase is not LifecyclePhase.GRACE_DRAIN
    ]
    repository = MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=EventSource(events),
        operations=OperationSource(
            operation(
                state.operation_id,
                status=OperationStatus.SUCCEEDED,
                traceparent="00-11111111111111111111111111111111-2222222222222222-01",
            )
        ),
        cluster="k8s-inference-h100",
    )

    await bridge.observe(state, attempt, pod_observation(attempt, observed_at=at(10)))
    await bridge.sync(state)

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and detail.rollup is not None
    assert detail.rollup.scheduler_occupied_gpu_seconds == 8
    assert detail.rollup.active_gpu_seconds == 3
    assert detail.rollup.phase_gpu_seconds["teardown"] == 0
    assert detail.rollup.phase_gpu_seconds["unclassified"] == 0
    assert detail.rollup.quality.value == "application_observed"
    assert detail.rollup.reconciled is True
    assert "phase_classification_incomplete" not in detail.rollup.data_gaps


@pytest.mark.asyncio
async def test_pod_correlations_are_stable_across_unscheduled_to_scheduled_enrichment() -> None:
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    scheduled = pod_observation(attempt, observed_at=at(10))
    scheduled_pod = scheduled.pod_lifecycle[0]
    unscheduled_pod = replace(
        scheduled_pod,
        observed_at=at(1),
        scheduled_at=None,
        gpu_uuids=(),
        device_allocation_observed_at=None,
        device_observation_resolution_seconds=0,
        completed_at=None,
        phases=(),
    )
    unscheduled = replace(scheduled, phases=(), pod_lifecycle=(unscheduled_pod,))
    repository = MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(operation(state.operation_id, status=OperationStatus.SUCCEEDED)),
        cluster="k8s-inference-h100",
    )

    await bridge.observe(state, attempt, unscheduled)
    await bridge.observe(state, attempt, scheduled)
    await bridge.observe(
        state,
        attempt,
        replace(
            scheduled,
            pod_lifecycle=(replace(scheduled_pod, observed_at=at(20)),),
        ),
    )

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None
    pod_key = f"scientific:{attempt.attempt_id}:pod:{scheduled_pod.pod_uid}"
    correlations = {value.correlation_key: value for value in detail.correlations}
    assert correlations[pod_key].observed_at == scheduled_pod.created_at
    assert correlations[f"{pod_key}:node:{scheduled_pod.node_uid}"].observed_at == scheduled_pod.scheduled_at
    assert (
        correlations[f"{pod_key}:device:{GPU_UUID}:0"].observed_at
        == scheduled_pod.device_allocation_observed_at
    )
    assert len(correlations) == 3

    changed_identity = replace(
        scheduled,
        pod_lifecycle=(replace(scheduled_pod, pod_name="fs2-design-target-a1-different"),),
    )
    with pytest.raises(ValueError, match="correlation key is already bound to different facts"):
        await bridge.observe(state, attempt, changed_identity)


@pytest.mark.asyncio
async def test_same_phase_times_from_retry_pods_keep_distinct_pod_identity() -> None:
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    first_observation = pod_observation(attempt, observed_at=at(10))
    first = first_observation.pod_lifecycle[0]
    second = replace(first, pod_uid="pod-uid-2", pod_name="fs2-design-target-a1-retry")
    observation = replace(
        first_observation,
        pod_uids=(first.pod_uid, second.pod_uid),
        pod_lifecycle=(first, second),
    )
    repository = MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(operation(state.operation_id, status=OperationStatus.SUCCEEDED)),
        cluster="k8s-inference-h100",
    )

    await bridge.observe(state, attempt, observation)
    await bridge.observe(state, attempt, observation)

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None
    assert {value.pod_uid for value in detail.correlations if value.pod_uid is not None} == {
        "pod-uid-1",
        "pod-uid-2",
    }
    phase_start_pods = {
        value.pod_uid
        for value in detail.signals
        if value.clock is LifecycleClock.PHASE and value.edge is LifecycleEdge.START
    }
    assert phase_start_pods == {"pod-uid-1", "pod-uid-2"}


@pytest.mark.asyncio
async def test_resumed_sync_keeps_pre_apply_events_stable_when_job_uid_is_enriched() -> None:
    terminal, terminal_attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    attempt = replace(
        terminal_attempt,
        workload=replace(terminal_attempt.workload, uid=None),
        completed_at=None,
        outcome=AttemptOutcome.ACTIVE,
        last_phase=LifecyclePhase.QUEUED,
        deletion_requested=False,
        resource_released=False,
        scheduling_admission=None,
        kueue_workload_uid=None,
        pod_uids=(),
    )
    state = replace(
        terminal,
        stages=(ScientificStageState(stage_id="design", status=StageStatus.ACTIVE, attempts=(attempt,)),),
        status=BatchStatus.RUNNING,
        revision=1,
        failure_code=None,
    )
    events = [
        BatchEvent(
            sequence=1,
            occurred_at=NOW,
            draft=BatchEventDraft.build(
                operation_id=state.operation_id,
                batch_id=state.batch_id,
                workload_id=state.workload_id,
                kind=BatchEventKind.LIFECYCLE,
                stage_id=attempt.stage_id,
                shard_id=attempt.shard_id,
                attempt_id=attempt.attempt_id,
                phase=LifecyclePhase.QUEUED,
            ),
        )
    ]
    repository = MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=EventSource(events),
        operations=OperationSource(operation(state.operation_id, status=OperationStatus.RUNNING)),
        cluster="k8s-inference-h100",
    )

    await bridge.sync(state)
    bound_attempt = replace(attempt, workload=replace(attempt.workload, uid="job-uid-1"))
    bound = replace(
        state,
        stages=(ScientificStageState(stage_id="design", status=StageStatus.ACTIVE, attempts=(bound_attempt,)),),
        revision=2,
    )
    await bridge.sync(bound)

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None
    assert {signal.job_uid for signal in detail.signals} == {None}
    assert {correlation.job_uid for correlation in detail.correlations if correlation.job_uid} == {"job-uid-1"}

    conflicting_attempt = replace(bound_attempt, workload=replace(bound_attempt.workload, uid="job-uid-2"))
    conflicting = replace(
        bound,
        stages=(
            ScientificStageState(
                stage_id="design",
                status=StageStatus.ACTIVE,
                attempts=(conflicting_attempt,),
            ),
        ),
        revision=3,
    )
    with pytest.raises(ValueError, match="event key is already bound to different facts"):
        await bridge.sync(conflicting)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [AttemptOutcome.PREEMPTED, AttemptOutcome.CANCELLED])
async def test_duplicate_restart_preemption_and_cancellation_never_double_charge_gpu_time(
    outcome: AttemptOutcome,
) -> None:
    state, attempt = terminal_state(outcome)
    events = lifecycle_events(state, attempt, preempted=outcome is AttemptOutcome.PREEMPTED)
    repository = MemoryLifecycleRepository()
    source = EventSource(events)
    operations = OperationSource(
        operation(
            state.operation_id,
            status=OperationStatus.CANCELLED if outcome is AttemptOutcome.CANCELLED else OperationStatus.FAILED,
        )
    )
    first = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=source,
        operations=operations,
        cluster="k8s-inference-h100",
        source_resolution_seconds=5,
    )
    observation = pod_observation(attempt, observed_at=at(10))

    before_node_uid = replace(
        observation,
        pod_lifecycle=(replace(observation.pod_lifecycle[0], node_uid=None),),
    )
    await first.observe(state, attempt, before_node_uid)
    repeated = replace(
        observation,
        pod_lifecycle=(replace(observation.pod_lifecycle[0], observed_at=at(11)),),
    )
    await first.observe(state, attempt, repeated)
    await first.sync(state)
    initial = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert initial is not None and initial.rollup is not None
    initial_counts = (len(initial.correlations), len(initial.signals))
    initial_digest = initial.rollup.events_sha256

    # A new bridge instance represents a controller restart. It replays every
    # durable batch event and repeats the latest immutable Pod observation.
    restarted = ScientificLifecycleBridge(
        lifecycle=repository,
        batches=source,
        operations=operations,
        cluster="k8s-inference-h100",
        source_resolution_seconds=5,
    )
    await restarted.sync(state)
    replayed = replace(
        observation,
        pod_lifecycle=(replace(observation.pod_lifecycle[0], observed_at=at(20)),),
    )
    await restarted.observe(state, attempt, replayed)
    await restarted.sync(state)

    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and detail.rollup is not None
    assert (len(detail.correlations), len(detail.signals)) == initial_counts
    assert detail.rollup.events_sha256 == initial_digest
    assert detail.subject.tenant_id == "oncology-a"
    assert detail.subject.model_id == "rfdiffusion"
    assert detail.subject.operation_id == state.operation_id
    assert detail.subject.batch_id == state.batch_id
    assert detail.subject.workload_id == state.workload_id
    assert detail.subject.attempt_id == attempt.attempt_id
    assert {value.job_uid for value in detail.correlations} >= {"job-uid-1"}
    assert {value.pod_uid for value in detail.correlations} >= {"pod-uid-1"}
    assert {value.node_uid for value in detail.correlations} >= {"node-uid-1"}
    assert {value.gpu_uuid for value in detail.correlations} >= {GPU_UUID}

    assert detail.rollup.quota_reserved_gpu_seconds == 11
    assert detail.rollup.scheduler_occupied_gpu_seconds == 8
    assert detail.rollup.device_allocated_gpu_seconds == 7
    assert detail.rollup.active_gpu_seconds == 3
    assert detail.rollup.phase_gpu_seconds["image_pull"] == 1
    assert detail.rollup.phase_gpu_seconds["artifact_load"] == 1
    assert detail.rollup.phase_gpu_seconds["restore"] == 1
    assert detail.rollup.phase_gpu_seconds["warmup"] == 1
    assert detail.rollup.phase_gpu_seconds["resident_idle"] == 1
    assert detail.rollup.phase_gpu_seconds["checkpoint_drain"] == 0
    assert detail.rollup.reconciled is True
    assert detail.rollup.quality.value == "application_observed"
    assert detail.rollup.terminal is True
    assert detail.rollup.outcome == outcome.value

    phases = {value.phase for value in detail.signals}
    assert {
        LedgerPhase.ENQUEUE,
        LedgerPhase.ADMISSION_WAIT,
        LedgerPhase.ADMIT,
        LedgerPhase.IMAGE_PULL,
        LedgerPhase.ARTIFACT_LOAD,
        LedgerPhase.RESTORE,
        LedgerPhase.WARMUP,
        LedgerPhase.ACTIVE_COMPUTE,
        LedgerPhase.RESIDENT_IDLE,
        LedgerPhase.COOLDOWN_GRACE,
        LedgerPhase.CHECKPOINT_DRAIN,
        LedgerPhase.TEARDOWN,
        LedgerPhase.RELEASE,
    }.issubset(phases)
    preemptions = [
        value
        for value in detail.signals
        if value.phase is LedgerPhase.PREEMPTION and value.edge is LifecycleEdge.INSTANT
    ]
    assert len(preemptions) == (1 if outcome is AttemptOutcome.PREEMPTED else 0)
    assert len([value for value in detail.signals if value.clock is LifecycleClock.DEVICE_ALLOCATED]) == 2
    device_signals = [
        value for value in detail.signals if value.clock is LifecycleClock.DEVICE_ALLOCATED
    ]
    assert next(value for value in device_signals if value.edge is LifecycleEdge.START).occurred_at == at(3)
    assert next(value for value in device_signals if value.edge is LifecycleEdge.END).occurred_at == at(10)
    assert {value.source_resolution_seconds for value in device_signals} == {5}
