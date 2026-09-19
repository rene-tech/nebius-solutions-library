"""Truthful phase/identity projection using the existing ledger and observer."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_scientific_admin_accounting import _row
from test_scientific_admin_postgres import _state
from test_scientific_lifecycle_bridge import at, pod_observation, terminal_state

from fs2_serve.lifecycle import LifecycleClock, LifecyclePhase, MeasurementQuality, _paired_intervals
from fs2_serve.scientific_admin_accounting import project_gpu_accounting
from fs2_serve.scientific_admin_phase_times import project_phase_times
from fs2_serve.scientific_admin_postgres import _placement, _stages
from fs2_serve.scientific_batch.kubernetes import HttpScientificBatchCluster
from fs2_serve.scientific_batch.lifecycle_bridge import ScientificLifecycleBridge
from fs2_serve.scientific_batch.models import (
    AttemptOutcome,
    StagePlacementClass,
    StageResourceEnvelope,
)


def test_startup_idle_cooldown_unknown_and_three_clocks_are_separate():
    state = _state()
    row = _row(state.stages[0].attempts[0].attempt_id, quota_reserved_gpu_seconds=140,
               device_allocated_gpu_seconds=95)
    result = project_gpu_accounting(state, [row, row])
    assert result.allocated.value == 100
    assert result.quota_reserved.value == 140  # Reservation is not device activity.
    assert result.device_allocated.value == 95
    assert result.sampled_device_activity.value is None
    assert result.phase_partition["artifact_load"].value == 25
    assert result.phase_partition["cooldown_grace"].value == 10
    assert result.phase_partition["unclassified"].value == 5
    assert result.phase_partition["resident_idle"].value is None
    assert sum(item.value or 0 for item in result.phase_partition.values()) == 100


def test_missing_device_and_scheduler_boundaries_do_not_become_zero():
    state = _state()
    row = _row(state.stages[0].attempts[0].attempt_id, device_allocated_gpu_seconds=0,
               data_gaps=["device_allocation_clock_missing", "scheduler_occupancy_clock_missing"])
    result = project_gpu_accounting(state, [row])
    assert result.device_allocated.value is None
    assert result.allocated.value is None
    assert result.active.evidence == "estimated"
    assert project_gpu_accounting(state, [{**row, "quality": "unavailable"}]) is None


def test_conflicting_attempt_rollup_is_not_silently_summed_or_overwritten():
    state = _state()
    row = _row(state.stages[0].attempts[0].attempt_id)
    with pytest.raises(ValueError, match="conflicting latest"):
        project_gpu_accounting(state, [row, {**row, "active_gpu_seconds": 9}])


def test_frozen_fit_includes_collector_and_does_not_promise_live_free_capacity():
    state = _state()
    stage = replace(state.plan.stages[0], placement_class=StagePlacementClass.ACCELERATOR,
                    resources=StageResourceEnvelope(cpu_millis=16000, memory_bytes=16*1024**3,
                                                    ephemeral_storage_bytes=8*1024**3,
                                                    limit_cpu_millis=16000, limit_memory_bytes=16*1024**3,
                                                    limit_ephemeral_storage_bytes=8*1024**3))
    state = replace(state, plan=replace(state.plan, stages=(stage,)), scheduling=replace(
        state.scheduling,
        stages=(replace(state.scheduling.stages[0], placement_class=StagePlacementClass.ACCELERATOR),),
    ))
    result = _placement(state, stage.stage_id)
    assert result.stage_cpu_millis == 16000
    assert result.pod_cpu_millis == 16100  # Cannot fit a 15.9 CPU node.
    assert result.pod_memory_bytes == 16*1024**3 + 256*1024**2
    assert result.eligible_pool_ids == ["h100-preemptible"]
    assert result.live_fit == "not-observed"
    assert result.scheduling_digest == state.scheduling.digest
    assert _placement(_state(), stage.stage_id).pod_cpu_millis is None


def event(pod_uid, reason, second, *, container="spec.containers{scientific-stage}", **updates):
    return {"metadata": {"uid": f"event-{reason}-{second}"}, "reason": reason,
            "involvedObject": {"kind": "Pod", "uid": pod_uid, "fieldPath": container},
            "eventTime": at(second).isoformat(), **updates}


def test_uid_fenced_pull_events_not_container_creating_establish_image_pull():
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    pod = pod_observation(attempt, observed_at=at(10)).pod_lifecycle[0]
    pairs = HttpScientificBatchCluster._image_pull_intervals([
        event(pod.pod_uid, "Pulling", 2), event(pod.pod_uid, "Pulled", 3),
        event("other-pod", "Pulling", 3), event("other-pod", "Pulled", 8),
    ], pod)
    assert len(pairs) == 1 and pairs[0].source_event_uids == ("event-Pulling-2", "event-Pulled-3")
    assert not HttpScientificBatchCluster._image_pull_intervals([
        event(pod.pod_uid, "Pulling", 2, container=None),
        event(pod.pod_uid, "Pulled", 3, container=None),
    ], pod)
    bridge = object.__new__(ScientificLifecycleBridge)
    bridge.cluster, bridge.source_resolution_seconds = "test-cluster", 1
    generic = replace(pod.phases[0], source_event_uids=())
    signals = bridge._pod_signals(state, attempt, replace(pod, phases=(generic, *pairs)))
    intervals, gaps = _paired_intervals(signals)
    assert not gaps
    pulls = [row for row in intervals if row.phase is LifecyclePhase.IMAGE_PULL]
    assert len(pulls) == 1 and pulls[0].quality is MeasurementQuality.MEASURED
    assert any(row.phase is LifecyclePhase.UNCLASSIFIED for row in intervals)
    assert any(row.phase is LifecyclePhase.NODE_REQUEST and row.clock is LifecycleClock.LIFECYCLE
               for row in intervals)
    durations = {row.phase: row.duration for row in project_phase_times(signals)}
    assert durations["image-pull"].value == 1
    assert durations["dispatch"].value is not None


@pytest.mark.asyncio
async def test_event_ingestion_is_one_bounded_read_and_rejects_aggregates_or_truncation():
    requests = []
    document = {"items": [event("pod", "Pulling", 2), event("pod", "Pulled", 3, count=2)]}

    async def request(method, path):
        requests.append((method, path))
        return httpx.Response(200, json=document)

    cluster = SimpleNamespace(_request=request)
    rows = await HttpScientificBatchCluster._pull_events(cluster, "models")
    assert len(requests) == 1 and "limit=1000" in requests[0][1]
    assert len(rows) == 1  # No invented first/last pair from repeated Event summaries.
    document["metadata"] = {"continue": "page-two"}
    assert await HttpScientificBatchCluster._pull_events(cluster, "models") == []


def test_retry_identity_projection_does_not_borrow_a_different_attempt_gpu():
    state = _state()
    original = state.stages[0].attempts[0]
    retry = replace(original, attempt_id=uuid4(), attempt_number=2)
    state = replace(state, stages=(replace(state.stages[0], attempts=(original, retry)),))
    correlations = (
        {"attempt_id": original.attempt_id, "pod_uid": "pod-first", "node_uid": "node-a", "gpu_uuid": "GPU-a"},
        {"attempt_id": retry.attempt_id, "pod_uid": "pod-retry", "node_uid": "node-b", "gpu_uuid": None},
        {"attempt_id": uuid4(), "pod_uid": "unrelated", "node_uid": "node-c", "gpu_uuid": "GPU-c"},
    )
    attempts = _stages(state, (), correlations=correlations)[0].attempts
    assert attempts[0].observed_gpu_uuids == ["GPU-a"]
    assert attempts[1].observed_gpu_uuids == []
    assert attempts[1].observed_node_uids == ["node-b"]
    assert attempts[1].node_count == 1
