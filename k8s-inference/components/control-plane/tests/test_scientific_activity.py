from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import respx
from test_scientific_lifecycle_bridge import (
    GPU_UUID,
    EventSource,
    OperationSource,
    at,
    lifecycle_events,
    operation,
    pod_observation,
    terminal_state,
)

from fs2_serve.admin_adapters import HttpPrometheusScalarReader
from fs2_serve.lifecycle import LifecycleRepository, MemoryLifecycleRepository
from fs2_serve.models import OperationStatus
from fs2_serve.scientific_activity import (
    ACTIVITY_DETAIL_KEYS,
    MAX_SERIES,
    METRIC,
    ScientificActivityCapture,
    activity_capture_reason,
    activity_summaries,
    summarize_matrix,
)
from fs2_serve.scientific_admin_postgres import _stages
from fs2_serve.scientific_batch.lifecycle_bridge import ScientificLifecycleBridge
from fs2_serve.scientific_batch.models import AttemptOutcome


class Reader:
    def __init__(self, matrix: Mapping[str, Any] | Exception):
        self.matrix = matrix
        self.calls = 0

    async def scientific_activity_matrix(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls += 1
        assert kwargs["from_at"] == at(3) and kwargs["to_at"] == at(10)
        if isinstance(self.matrix, Exception):
            raise self.matrix
        return self.matrix


async def fixture(repository: LifecycleRepository | None = None):
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    repository = repository or MemoryLifecycleRepository()
    bridge = ScientificLifecycleBridge(
        lifecycle=repository, batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(operation(state.operation_id, status=OperationStatus.SUCCEEDED)),
    )
    await bridge.observe(state, attempt, pod_observation(attempt, observed_at=at(10)))
    await bridge.sync(state)
    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None
    matrix = {"resultType": "matrix", "result": [{
        "metric": {
            "__name__": METRIC, "fs2_nebius_ai_operation_id": str(state.operation_id),
            "fs2_nebius_ai_attempt_id": str(attempt.attempt_id), "fs2_nebius_ai_model_id": state.model_id,
            "fs2_nebius_ai_tenant_id": state.tenant_id, "fs2_nebius_ai_stage_id": attempt.stage_id,
            "pod_uid": "pod-uid-1", "pod": "fs2-design-target-a1-abcde", "namespace": "fs2-scientific",
            "UUID": GPU_UUID,
        },
        # Two samples outside the allocation are not joined; no interpolation.
        "values": [[at(t).timestamp(), str(value)] for t, value in [(2, 99), (4, 0), (7, 80), (9, 0), (11, 100)]],
    }]}
    return repository, bridge, state, attempt, detail, matrix


async def verify_durable_capture(repository: LifecycleRepository | None = None) -> None:
    repository, bridge, state, attempt, before, matrix = await fixture(repository)
    reader = Reader(matrix)
    bridge.activity = ScientificActivityCapture(reader=reader, lifecycle=repository, clock=lambda: at(13))
    await bridge.sync(state)
    await bridge.sync(state)
    assert reader.calls == 1
    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and detail.rollup is not None and before.rollup is not None
    values = activity_summaries(detail.signals)
    assert len(values) == 1
    value = values[0]
    assert value.sample_count == 3 and value.zero_samples == 2 and value.positive_samples == 1
    assert value.phase_samples == {"restore": 1, "active_compute": 1, "resident_idle": 1}
    assert value.phase_zero_samples == {"restore": 1, "resident_idle": 1}
    assert value.phase_positive_samples == {"active_compute": 1}
    assert value.max_gap_seconds == 3 and value.min_percent == 0 and value.max_percent == 80
    assert value.first_sample_at == at(4) and value.last_sample_at == at(9)
    assert value.node_uid == "node-uid-1"
    assert set(value.signal_detail()).issubset(ACTIVITY_DETAIL_KEYS)
    assert activity_capture_reason(detail.signals) == "dcgm_capture_observed"
    # Device samples never change existing allocation, execution, idle or retry accounting.
    for field in ("quota_reserved_gpu_seconds", "scheduler_occupied_gpu_seconds", "device_allocated_gpu_seconds",
                  "active_gpu_seconds", "occupied_idle_gpu_seconds", "phase_gpu_seconds"):
        assert getattr(detail.rollup, field) == getattr(before.rollup, field)
    assert _stages(state, (), tuple(detail.signals))[0].attempts[0].device_activity == values


@pytest.mark.asyncio
async def test_capture_is_durable_idempotent_and_does_not_change_gpu_seconds() -> None:
    await verify_durable_capture()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "fs2_nebius_ai_operation_id", "fs2_nebius_ai_attempt_id", "fs2_nebius_ai_model_id",
    "fs2_nebius_ai_tenant_id", "fs2_nebius_ai_stage_id", "pod_uid", "UUID", "pod", "namespace",
])
async def test_identity_mismatch_never_selects_another_pod(key: str) -> None:
    _, _, state, attempt, detail, matrix = await fixture()
    matrix["result"][0]["metric"][key] = "mismatch"
    signals, rejected = summarize_matrix(
        matrix, state=state, attempt=attempt, signals=detail.signals,
        correlations=detail.correlations, observed_at=at(13),
    )
    assert signals == [] and rejected == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_duplicate_exporters_deduplicate_identical_points_and_reject_conflicting_device(conflict: bool) -> None:
    _, _, state, attempt, detail, matrix = await fixture()
    duplicate = copy.deepcopy(matrix["result"][0])
    duplicate["metric"]["instance"] = "different-exporter"
    if conflict:
        duplicate["values"][2][1] = "10"
    matrix["result"].append(duplicate)
    signals, rejected = summarize_matrix(
        matrix, state=state, attempt=attempt, signals=detail.signals,
        correlations=detail.correlations, observed_at=at(13),
    )
    assert rejected == int(conflict)
    assert not signals if conflict else activity_summaries(signals)[0].sample_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("matrix", [RuntimeError("private transport exception"), {"resultType": "vector", "result": []},
                                  {"resultType": "matrix", "result": []}])
async def test_unavailable_capture_is_explicit_and_does_not_fail_or_repeat_completed_work(matrix) -> None:
    repository, bridge, state, attempt, before, _ = await fixture()
    reader = Reader(matrix)
    bridge.activity = ScientificActivityCapture(reader=reader, lifecycle=repository, clock=lambda: at(13))
    await bridge.sync(state)
    await bridge.sync(state)
    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and detail.rollup is not None and before.rollup is not None
    assert not activity_summaries(detail.signals) and reader.calls == 1
    assert activity_capture_reason(detail.signals) in {"dcgm_source_unavailable", "dcgm_no_verified_samples"}
    assert "private transport" not in detail.model_dump_json()
    assert detail.rollup.outcome == "succeeded" and detail.rollup.terminal
    assert detail.rollup.device_allocated_gpu_seconds == before.rollup.device_allocated_gpu_seconds


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["NaN", "Inf", "-1", "101"])
async def test_invalid_utilization_rejected(value: str) -> None:
    _, _, state, attempt, detail, matrix = await fixture()
    matrix["result"][0]["values"][1][1] = value
    with pytest.raises(ValueError, match="sample is invalid"):
        summarize_matrix(
            matrix, state=state, attempt=attempt, signals=detail.signals,
            correlations=detail.correlations, observed_at=at(13),
        )


@pytest.mark.asyncio
async def test_gpu_annotation_pair_is_required_even_with_matching_metric_labels() -> None:
    _, _, state, attempt, detail, matrix = await fixture()
    signals, rejected = summarize_matrix(
        matrix, state=state, attempt=attempt, signals=detail.signals, correlations=[], observed_at=at(13),
    )
    assert signals == [] and rejected == 1


@pytest.mark.asyncio
async def test_release_boundary_and_source_series_bound_are_not_silently_extended() -> None:
    _, _, state, attempt, detail, matrix = await fixture()
    matrix["result"][0]["values"].append([at(10).timestamp(), "100"])
    signals, _ = summarize_matrix(
        matrix, state=state, attempt=attempt, signals=detail.signals,
        correlations=detail.correlations, observed_at=at(13),
    )
    assert activity_summaries(signals)[0].sample_count == 3
    matrix["result"] *= MAX_SERIES + 1
    with pytest.raises(ValueError, match="outside the bound"):
        summarize_matrix(matrix, state=state, attempt=attempt, signals=detail.signals,
                         correlations=detail.correlations, observed_at=at(13))


@pytest.mark.asyncio
async def test_no_gpu_allocation_skips_query_and_keeps_explicit_unknown() -> None:
    state, attempt = terminal_state(AttemptOutcome.SUCCEEDED)
    repository = MemoryLifecycleRepository()
    reader = Reader(RuntimeError("must not query"))
    bridge = ScientificLifecycleBridge(
        lifecycle=repository, batches=EventSource(lifecycle_events(state, attempt, preempted=False)),
        operations=OperationSource(operation(state.operation_id, status=OperationStatus.SUCCEEDED)),
        activity=ScientificActivityCapture(reader=reader, lifecycle=repository, clock=lambda: at(13)),
    )
    await bridge.sync(state)
    detail = await repository.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
    assert detail is not None and reader.calls == 0
    assert activity_capture_reason(detail.signals) == "dcgm_no_verified_allocation"


@pytest.mark.asyncio
@respx.mock
async def test_prometheus_uses_raw_matrix_instant_query_not_resampled_range() -> None:
    route = respx.post("http://prometheus.fs2-observability.svc:9090/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}),
    )
    operation_id, attempt_id = uuid4(), uuid4()
    reader = HttpPrometheusScalarReader("http://prometheus.fs2-observability.svc:9090")
    result = await reader.scientific_activity_matrix(
        operation_id=operation_id, attempt_id=attempt_id, from_at=at(3), to_at=at(10),
    )
    assert result == {"resultType": "matrix", "result": []}
    assert route.call_count == 1
    form = parse_qs(route.calls[0].request.content.decode())
    assert form["query"] == [
        f'{METRIC}{{fs2_nebius_ai_operation_id="{operation_id}",fs2_nebius_ai_attempt_id="{attempt_id}"}}[7s]',
    ]
    assert "step" not in form and form["time"] == [at(10).isoformat()]
