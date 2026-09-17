"""Regression contracts for SAI-21 scientific GPU reservation settlement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fs2_serve.scientific_batch.accounting import settle_scientific_gpu_reservation
from fs2_serve.scientific_batch.models import (
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
    WorkloadRef,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def admitted_state(*, admission: SchedulingAdmission | None) -> ScientificBatchState:
    plan = ScientificBatchPlan(
        stages=(ScientificStagePlan(stage_id="inference", resource_class=ResourceClass.GPU),)
    )
    scheduling = SchedulingSnapshot(
        policy_revision="sai-21-accounting-v1",
        captured_at=NOW,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="tenant-a-gpu",
        model_lane="protein-design",
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id="inference",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="tenant-a-gpu",
                workload_priority_class="fs2-customer-batch",
                workload_priority_value=500,
                resolved_pool_preference=("h100-preemptible",),
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=2,
                max_queue_seconds=600,
                max_execution_seconds=3600,
                checkpoint_mode=CheckpointMode.RESTART,
                preemption_mode=PreemptionMode.RESTARTABLE,
            ),
        ),
    )
    state = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="tenant-a",
        model_id="protein-design",
        variant_id="sai-21-accounting",
        input_artifact_id=uuid4(),
        plan=plan,
        scheduling=scheduling,
    )
    if admission is None:
        return replace(
            state,
            status=BatchStatus.CANCELLED,
            stages=(replace(state.stages[0], status=StageStatus.CANCELLED),),
        )
    attempt = ScientificAttemptState(
        attempt_id=uuid4(),
        stage_id="inference",
        shard_id="main",
        attempt_number=1,
        workload=WorkloadRef(
            namespace="fs2-models",
            name="sai-21-accounting",
            kind=WorkloadKind.JOB,
            uid="job-sai-21-accounting",
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=35),
        outcome=AttemptOutcome.SUCCEEDED,
        last_phase=LifecyclePhase.TEARDOWN,
        deletion_requested=True,
        resource_released=True,
        scheduling_admission=admission,
    )
    return replace(
        state,
        status=BatchStatus.SUCCEEDED,
        stages=(
            replace(
                state.stages[0],
                status=StageStatus.SUCCEEDED,
                attempts=(attempt,),
            ),
        ),
    )


def test_cancellation_before_gpu_admission_releases_the_full_reservation() -> None:
    settlement = settle_scientific_gpu_reservation(
        admitted_state(admission=None),
        reserved_gpu_seconds=7200,
    )

    assert settlement.charged_gpu_seconds == 0
    assert settlement.released_gpu_seconds == 7200
    assert settlement.reason == "no_gpu_execution"
    assert settlement.evidence_complete is True


def test_permanent_materialization_failure_before_gpu_admission_charges_zero() -> None:
    queued = admitted_state(admission=None)
    failed = replace(
        queued,
        status=BatchStatus.FAILED,
        failure_code="artifact_materialization_failed",
        stages=(
            replace(
                queued.stages[0],
                status=StageStatus.FAILED,
                failure_code="artifact_materialization_failed",
            ),
        ),
    )

    settlement = settle_scientific_gpu_reservation(
        failed,
        reserved_gpu_seconds=7200,
    )

    assert settlement.charged_gpu_seconds == 0
    assert settlement.released_gpu_seconds == 7200
    assert settlement.reason == "no_gpu_execution"
    assert settlement.evidence_complete is True


@pytest.mark.parametrize("terminal_status", [BatchStatus.FAILED, BatchStatus.CANCELLED])
def test_persisted_gpu_attempt_without_admission_or_uid_is_fail_safe_charged(
    terminal_status: BatchStatus,
) -> None:
    state = admitted_state(admission=None)
    attempt = ScientificAttemptState(
        attempt_id=uuid4(),
        stage_id="inference",
        shard_id="main",
        attempt_number=1,
        workload=WorkloadRef(
            namespace="fs2-models",
            name="sai-21-apply-crash",
            kind=WorkloadKind.JOB,
            uid=None,
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        outcome=AttemptOutcome.FAILED,
        last_phase=LifecyclePhase.SCHEDULING,
        scheduling_admission=None,
    )
    terminal = replace(
        state,
        status=terminal_status,
        stages=(
            replace(
                state.stages[0],
                status=(StageStatus.FAILED if terminal_status is BatchStatus.FAILED else StageStatus.CANCELLED),
                attempts=(attempt,),
            ),
        ),
    )

    settlement = settle_scientific_gpu_reservation(terminal, reserved_gpu_seconds=7200)

    assert settlement.charged_gpu_seconds == 7200
    assert settlement.released_gpu_seconds == 0
    assert settlement.reason == "missing_evidence_fail_safe"
    assert settlement.evidence_complete is False


def test_observed_kueue_gpu_occupancy_is_charged_and_the_remainder_released() -> None:
    settlement = settle_scientific_gpu_reservation(
        admitted_state(
            admission=SchedulingAdmission(
                resolved_pool_id="h100-preemptible",
                admitted_resource_flavor="h100-preemptible",
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=2,
                quota_reserved_at=NOW,
                admitted_at=NOW + timedelta(seconds=5),
            )
        ),
        reserved_gpu_seconds=7200,
    )

    assert settlement.charged_gpu_seconds == 70
    assert settlement.released_gpu_seconds == 7130
    assert settlement.reason == "observed_execution"
    assert settlement.evidence_complete is True


def test_missing_gpu_lifecycle_evidence_retains_only_the_bounded_reservation() -> None:
    settlement = settle_scientific_gpu_reservation(
        admitted_state(
            admission=SchedulingAdmission(
                resolved_pool_id="h100-preemptible",
                admitted_resource_flavor="h100-preemptible",
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=2,
                quota_reserved_at=None,
                admitted_at=None,
            )
        ),
        reserved_gpu_seconds=7200,
    )

    assert settlement.charged_gpu_seconds == 7200
    assert settlement.released_gpu_seconds == 0
    assert settlement.reason == "missing_evidence_fail_safe"
    assert settlement.evidence_complete is False


def test_success_without_any_gpu_admission_is_bounded_missing_evidence() -> None:
    cancelled = admitted_state(admission=None)
    malformed_success = replace(
        cancelled,
        status=BatchStatus.SUCCEEDED,
        stages=(replace(cancelled.stages[0], status=StageStatus.SUCCEEDED),),
    )

    settlement = settle_scientific_gpu_reservation(
        malformed_success,
        reserved_gpu_seconds=7200,
    )

    assert settlement.charged_gpu_seconds == 7200
    assert settlement.released_gpu_seconds == 0
    assert settlement.reason == "missing_evidence_fail_safe"
    assert settlement.evidence_complete is False


def test_nonterminal_interruption_cannot_settle_before_controller_cleanup() -> None:
    cancelled = admitted_state(admission=None)
    queued = replace(
        cancelled,
        status=BatchStatus.QUEUED,
        stages=(replace(cancelled.stages[0], status=StageStatus.PENDING),),
    )

    with pytest.raises(ValueError, match="terminal state"):
        settle_scientific_gpu_reservation(queued, reserved_gpu_seconds=7200)
