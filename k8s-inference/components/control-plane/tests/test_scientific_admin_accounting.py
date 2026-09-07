from dataclasses import replace
from uuid import uuid4

from test_scientific_admin_postgres import NOW, _state

from fs2_serve.scientific_admin_accounting import project_gpu_accounting
from fs2_serve.scientific_admin_postgres import _admission_state, _attempt, _run_status, _shard_counts, _stage_status
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    BatchEvent,
    BatchEventDraft,
    BatchEventKind,
    LifecyclePhase,
)


def _row(attempt_id, **updates):
    return {
        "attempt_id": attempt_id,
        "rollup_id": uuid4(),
        "quality": "measured",
        "terminal": True,
        "reconciled": True,
        "scheduler_occupied_gpu_seconds": 100.0,
        "active_gpu_seconds": 60.0,
        "occupied_idle_gpu_seconds": 40.0,
        "reconciliation_delta_seconds": 0.0,
        "phase_gpu_seconds": {
            "active_compute": 60.0,
            "artifact_load": 25.0,
            "cooldown_grace": 10.0,
            "unclassified": 5.0,
        },
        **updates,
    }


def test_exact_attempt_rollups_partition_occupied_time_without_double_counting():
    state = _state()
    attempt = state.stages[0].attempts[0]
    row = _row(attempt.attempt_id)
    # An unrelated/CPU attempt and duplicate fetched row must not inflate totals.
    result = project_gpu_accounting(state, [row, row, _row(uuid4())])
    assert result is not None
    assert result.allocated.value == 100
    assert result.active.value == 60
    assert result.idle_total.value == 30
    assert result.grace_drain.value == 10
    assert result.reconciliation_delta.value == 0
    assert result.allocated.evidence == "measured"
    assert {item.cause: item.duration.value for item in result.idle_by_cause} == {
        "artifact-load": 25,
        "unattributed": 5,
    }


def test_missing_and_application_observed_rollups_are_not_invented_exact_measurements():
    state = _state()
    attempt = state.stages[0].attempts[0]
    assert project_gpu_accounting(state, []) is None
    assert project_gpu_accounting(state, [{"attempt_id": attempt.attempt_id, "rollup_id": None}]) is None
    result = project_gpu_accounting(state, [_row(attempt.attempt_id, quality="application_observed", reconciled=False)])
    assert result is not None
    assert result.active.value == 60
    assert result.active.evidence == "estimated"
    assert "application-observed" in result.active.reason


def test_node_pending_is_not_running_and_exposes_the_bounded_scheduler_reason():
    state = _state()
    attempt = replace(state.stages[0].attempts[0], last_phase=LifecyclePhase.NODE_PENDING)
    state = replace(state, stages=(replace(state.stages[0], attempts=(attempt,)),))
    event = BatchEvent(
        sequence=5,
        occurred_at=NOW,
        draft=BatchEventDraft.build(
            operation_id=state.operation_id,
            batch_id=state.batch_id,
            workload_id=state.workload_id,
            kind=BatchEventKind.LIFECYCLE,
            attempt_id=attempt.attempt_id,
            stage_id=attempt.stage_id,
            phase=LifecyclePhase.NODE_PENDING,
            code="UnschedulableInsufficientCpu",
        ),
    )
    result = _attempt(attempt, (event,), resource_class=state.plan.stages[0].resource_class)
    assert result.status == "queued"
    assert result.phase == "node_pending"
    assert "full CPU request, including sidecars" in result.phase_reason
    assert result.error is None
    assert _run_status(state) == "admitted"
    assert _stage_status(state.stages[0]) == "admitted"


def test_mixed_shard_admission_ignores_superseded_retry_instead_of_last_array_element():
    state = _state()
    first = state.stages[0].attempts[0]
    pending = replace(
        first, attempt_id=uuid4(), shard_id="waiting", scheduling_admission=None, last_phase=LifecyclePhase.SCHEDULING
    )
    preempted = replace(
        first,
        attempt_id=uuid4(),
        shard_id="retried",
        outcome=AttemptOutcome.PREEMPTED,
        completed_at=NOW,
        last_phase=LifecyclePhase.PREEMPTED,
    )
    retried = replace(first, attempt_id=uuid4(), shard_id="retried", attempt_number=2)
    stage = replace(state.stages[0], attempts=(first, preempted, retried, pending))
    state = replace(state, stages=(stage,))
    assert _shard_counts(stage) == {"running": 2, "pending": 1}
    assert _stage_status(stage) == "running"
    admission, reason = _admission_state(state)
    assert admission == "admitted"
    assert "1 pending" in reason and "2 running" in reason
    assert "not observed" not in reason
