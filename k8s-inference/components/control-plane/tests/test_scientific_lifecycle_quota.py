"""Attempt-level quota counts and append-only compatibility for scientific gangs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from test_gromacs_execution_shapes import compile_and_freeze, resource_for
from test_scientific_lifecycle_bridge import (
    EventSource,
    OperationSource,
    at,
    lifecycle_events,
    operation,
    pod_observation,
    terminal_state,
)

from fs2_serve.lifecycle import LifecycleClock, LifecycleEdge, LifecycleSignal, MemoryLifecycleRepository
from fs2_serve.models import OperationStatus
from fs2_serve.scientific_batch.lifecycle_bridge import ScientificLifecycleBridge
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    BatchEvent,
    BatchStatus,
    ExecutionMode,
    LifecyclePhase,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStageState,
    StageStatus,
    WorkloadKind,
)
from fs2_serve.scientific_batch.podset_envelope import compare_kueue_usage, envelope_from_manifest


def shaped_state(nodes: int, gpus: int) -> tuple[ScientificBatchState, ScientificAttemptState]:
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    assert attempt.scheduling_admission is not None
    attempt = replace(
        attempt,
        shard_id=None if nodes > 1 else "gang",
        workload=replace(attempt.workload, kind=WorkloadKind.JOB_SET if nodes > 1 else WorkloadKind.JOB),
        scheduling_admission=replace(attempt.scheduling_admission, accelerator_count=gpus),
        pod_uids=tuple(f"pod-uid-{index + 1}" for index in range(nodes)),
    )
    stage = replace(
        state.plan.stages[0],
        mode=ExecutionMode.TRUE_GANG if nodes > 1 else ExecutionMode.FANOUT,
        shards=("gang",),
        gang_size=nodes if nodes > 1 else None,
    )
    state = replace(
        state,
        plan=ScientificBatchPlan(stages=(stage,)),
        scheduling=replace(state.scheduling, stages=(replace(state.scheduling.stages[0], accelerator_count=gpus),)),
        stages=(ScientificStageState(stage_id="design", status=StageStatus.SUCCEEDED, attempts=(attempt,)),),
    )
    return state, attempt


def bridge_for(state, attempt, repository, *, bridge_type=ScientificLifecycleBridge):
    return bridge_type(
        lifecycle=repository,
        batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(
            operation(state.operation_id, status=OperationStatus.SUCCEEDED).model_copy(
                update={"token_id": state.operation_id}
            )
        ),
        cluster="test-cluster",
    )


class LegacyQuotaBridge(ScientificLifecycleBridge):
    """Exactly the old event projection, without changing persistence/reconciliation."""

    def _event_signals(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        events: Sequence[BatchEvent],
    ) -> list[LifecycleSignal]:
        assert attempt.scheduling_admission is not None
        return [
            signal.model_copy(update={"gpu_count": attempt.scheduling_admission.accelerator_count})
            if signal.clock is LifecycleClock.QUOTA_RESERVED
            else signal
            for signal in super()._event_signals(state, attempt, events)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes,gpus", [(1, 1), (1, 4), (2, 1), (2, 8)])
async def test_quota_count_matches_compiled_plan_rendered_pods_and_kueue_aggregate(tmp_path: Path, nodes, gpus):
    runtime, _, compiled, snapshot, _ = compile_and_freeze(tmp_path, nodes, gpus)
    resource = resource_for(compiled, snapshot, nodes)
    envelope = envelope_from_manifest(runtime.render(resource), resource.kind)
    assignments = [
        {
            "name": envelope.names[0],
            "count": nodes,
            "resourceUsage": {"nvidia.com/gpu": str(nodes * gpus)},
        }
    ]
    admission = compare_kueue_usage(envelope, assignments, accelerator_resource="nvidia.com/gpu")
    assert admission.accelerator_per_replica == gpus
    assert admission.accelerator_aggregate == nodes * gpus

    state, attempt = shaped_state(nodes, admission.accelerator_per_replica)
    # Keep the actual compiler's frozen mode, gang size and selected envelope,
    # including the legacy no-shape 2x1 path, rather than testing arithmetic alone.
    state = replace(
        state,
        plan=ScientificBatchPlan(stages=(replace(compiled.controller_plan.stages[0], stage_id="design"),)),
        scheduling=replace(snapshot, stages=(replace(snapshot.stages[0], stage_id="design"),)),
    )
    repository = MemoryLifecycleRepository()
    bridge = bridge_for(state, attempt, repository)
    observation = pod_observation(attempt, observed_at=at(10))
    pods = tuple(
        replace(
            observation.pod_lifecycle[0],
            pod_uid=uid,
            pod_name=f"scientific-gang-{index}",
            node_name=f"node-{index}",
            node_uid=f"node-uid-{index}",
            gpu_count=gpus,
            gpu_uuids=(),
            device_allocation_observed_at=None,
            device_observation_resolution_seconds=0,
        )
        for index, uid in enumerate(attempt.pod_uids)
    )
    await bridge.observe(state, attempt, replace(observation, pod_uids=attempt.pod_uids, pod_lifecycle=pods))
    await bridge.sync(state)
    first = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert first is not None and first.rollup is not None
    quota = [signal for signal in first.signals if signal.clock is LifecycleClock.QUOTA_RESERVED]
    assert len(quota) == 2
    assert {signal.gpu_count for signal in quota} == {admission.accelerator_aggregate}
    assert first.rollup.quota_reserved_gpu_seconds == 11 * nodes * gpus
    assert first.rollup.scheduler_occupied_gpu_seconds == 8 * nodes * gpus
    assert first.rollup.active_gpu_seconds == 3 * nodes * gpus
    await bridge_for(state, attempt, repository).sync(state)
    repeated = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert repeated is not None and repeated.rollup is not None
    assert repeated.signals == first.signals
    assert repeated.rollup.events_sha256 == first.rollup.events_sha256


@pytest.mark.asyncio
async def test_independent_fanout_shards_and_literal_gang_do_not_multiply_attempt_quota():
    state, attempt = shaped_state(1, 4)
    state = replace(
        state,
        plan=ScientificBatchPlan(stages=(replace(state.plan.stages[0], shards=("gang", "second", "third")),)),
    )
    repository = MemoryLifecycleRepository()
    await bridge_for(state, attempt, repository).sync(state)
    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and detail.rollup is not None
    assert detail.rollup.quota_reserved_gpu_seconds == 44


@pytest.mark.asyncio
@pytest.mark.parametrize("gpus", [1, 8])
async def test_completed_legacy_quota_edges_and_rollup_stay_immutable(gpus):
    state, attempt = shaped_state(2, gpus)
    repository = MemoryLifecycleRepository()
    await bridge_for(state, attempt, repository, bridge_type=LegacyQuotaBridge).sync(state)
    before = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert before is not None and before.rollup is not None
    assert before.rollup.quota_reserved_gpu_seconds == 11 * gpus

    await bridge_for(state, attempt, repository).sync(state)
    after = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert after is not None and after.rollup is not None
    assert after.signals == before.signals
    assert after.rollup.events_sha256 == before.rollup.events_sha256
    assert after.rollup.quota_reserved_gpu_seconds == before.rollup.quota_reserved_gpu_seconds


@pytest.mark.asyncio
@pytest.mark.parametrize("gpus", [1, 8])
async def test_open_legacy_interval_closes_with_original_count_without_extra_interval(gpus):
    terminal, attempt = shaped_state(2, gpus)
    active_attempt = replace(
        attempt,
        completed_at=None,
        outcome=AttemptOutcome.ACTIVE,
        last_phase=LifecyclePhase.ACTIVE_COMPUTE,
        deletion_requested=False,
        resource_released=False,
    )
    active = replace(
        terminal,
        status=BatchStatus.RUNNING,
        stages=(ScientificStageState(stage_id="design", status=StageStatus.ACTIVE, attempts=(active_attempt,)),),
    )
    repository = MemoryLifecycleRepository()
    legacy = bridge_for(active, active_attempt, repository, bridge_type=LegacyQuotaBridge)
    legacy.batches.events = [event for event in legacy.batches.events if event.occurred_at < at(10)]
    await legacy.sync(active)
    before = await repository.get_workload(attempt.attempt_id, tenant_id=active.tenant_id)
    assert before is not None
    old_quota = [signal for signal in before.signals if signal.clock is LifecycleClock.QUOTA_RESERVED]
    assert len(old_quota) == 1 and old_quota[0].edge is LifecycleEdge.START

    await bridge_for(terminal, attempt, repository).sync(terminal)
    await bridge_for(terminal, attempt, repository).sync(terminal)
    after = await repository.get_workload(attempt.attempt_id, tenant_id=terminal.tenant_id)
    assert after is not None and after.rollup is not None
    quota = [signal for signal in after.signals if signal.clock is LifecycleClock.QUOTA_RESERVED]
    assert len(quota) == 2 and len({signal.interval_key for signal in quota}) == 1
    assert {signal.gpu_count for signal in quota} == {gpus}
    assert next(signal for signal in quota if signal.edge is LifecycleEdge.START) == old_quota[0]
    assert after.rollup.quota_reserved_gpu_seconds == 11 * gpus
    retained = {signal.event_key: signal for signal in after.signals}
    assert all(retained[signal.event_key] == signal for signal in before.signals)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changed_fact", ["count", "time", "resolution"])
async def test_quota_compatibility_does_not_accept_unrelated_drift(legacy, changed_fact):
    state, attempt = shaped_state(2, 1)
    repository = MemoryLifecycleRepository()
    first_type = LegacyQuotaBridge if legacy else ScientificLifecycleBridge
    await bridge_for(state, attempt, repository, bridge_type=first_type).sync(state)
    bridge = bridge_for(state, attempt, repository)
    assert attempt.scheduling_admission is not None
    if changed_fact == "resolution":
        bridge.source_resolution_seconds = 1
    else:
        changed = replace(
            attempt.scheduling_admission,
            **({"accelerator_count": 3} if changed_fact == "count" else {"quota_reserved_at": at(0)}),
        )
        state = replace(
            state,
            stages=(replace(state.stages[0], attempts=(replace(attempt, scheduling_admission=changed),)),),
        )
    with pytest.raises(ValueError, match="event key is already bound to different facts"):
        await bridge.sync(state)
