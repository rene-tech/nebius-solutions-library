from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from fs2_serve.scientific_batch.kubernetes import HttpScientificBatchCluster, _pending_code, _pod_lifecycle
from fs2_serve.scientific_batch.models import LifecyclePhase, PodPhaseInterval
from fs2_serve.scientific_batch.startup_telemetry import snapshot_request_started

NOW = datetime(2026, 9, 7, 15, 30, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def pod(*, snapshot: bool = True, terminal: bool = False) -> dict:
    return {
        "metadata": {"uid": "pod-1", "name": "stage-1", "creationTimestamp": NOW.isoformat()},
        "spec": {"nodeName": "node-1", "containers": [{
            "name": "scientific-stage",
            "command": ["python", "/snapshot-source/supervisor.py", "restore"] if snapshot else ["python"],
            "resources": {"requests": {"nvidia.com/gpu": "1"}},
        }]},
        "status": {
            "phase": "Succeeded" if terminal else "Running",
            "conditions": [{"type": "PodScheduled", "status": "True", "lastTransitionTime": at(1).isoformat()}],
            "containerStatuses": [{"name": "scientific-stage", "state": {
                "terminated" if terminal else "running": {
                    "startedAt": at(2).isoformat(),
                    **({"finishedAt": at(20).isoformat(), "exitCode": 0} if terminal else {}),
                },
            }}],
        },
    }


@pytest.mark.parametrize("mechanism", ["cuda-criu-restored", "normal-load-fallback"])
def test_exact_timestamped_snapshot_marker(mechanism: str) -> None:
    line = f'{at(7).isoformat()} {json.dumps({"event": "scientific_snapshot_request", "mechanism": mechanism})}'
    assert snapshot_request_started(f"unrelated output\n{line}\n") == at(7)


@pytest.mark.parametrize("line", [
    'not-a-time {"event":"scientific_snapshot_request","mechanism":"cuda-criu-restored"}',
    '2026-09-07T15:30:07 {"event":"scientific_snapshot_request","mechanism":"cuda-criu-restored"}',
    '2026-09-07T15:30:07Z {"event":"scientific_snapshot_request","mechanism":"selected"}',
    '2026-09-07T15:30:07Z {"message":"scientific_snapshot_request"}',
])
def test_snapshot_marker_does_not_infer_success_from_selection_or_invalid_time(line: str) -> None:
    assert snapshot_request_started(line) is None


@pytest.mark.parametrize("terminal", [False, True])
def test_snapshot_startup_is_separate_from_active_compute(terminal: bool) -> None:
    observation = _pod_lifecycle(
        pod(terminal=terminal), accelerator_resource_name="nvidia.com/gpu",
        observed_at=at(25), snapshot_request_at=at(7),
    )
    assert observation is not None
    assert PodPhaseInterval(LifecyclePhase.RESTORING, at(2), at(7)) in observation.phases
    assert PodPhaseInterval(LifecyclePhase.ACTIVE_COMPUTE, at(7), at(20) if terminal else None) in observation.phases
    assert observation.gpu_count == 1


@pytest.mark.parametrize("marker", [None, at(0), at(30)])
def test_missing_or_out_of_bounds_marker_preserves_occupancy_without_invented_compute(marker) -> None:
    observation = _pod_lifecycle(
        pod(), accelerator_resource_name="nvidia.com/gpu", observed_at=at(25), snapshot_request_at=marker,
    )
    assert observation is not None and observation.scheduled_at == at(1)
    assert not {LifecyclePhase.RESTORING, LifecyclePhase.ACTIVE_COMPUTE} & {v.phase for v in observation.phases}


def test_native_runtime_does_not_require_snapshot_marker() -> None:
    observation = _pod_lifecycle(pod(snapshot=False), accelerator_resource_name="nvidia.com/gpu", observed_at=at(25))
    assert observation is not None
    assert PodPhaseInterval(LifecyclePhase.ACTIVE_COMPUTE, at(2)) in observation.phases


@pytest.mark.parametrize(("message", "code"), [
    ("0/4 nodes available: 2 Insufficient cpu", "UnschedulableInsufficientCpu"),
    ("Insufficient memory", "UnschedulableInsufficientMemory"),
    ("Insufficient nvidia.com/gpu", "UnschedulableInsufficientGpu"),
    ("didn't match Pod's node affinity/selector", "UnschedulableNodeAffinity"),
    ("sensitive unrelated message", "NodeProvisioning"),
])
def test_pending_scheduler_reason_is_bounded(message: str, code: str) -> None:
    status = {"conditions": [{"type": "PodScheduled", "status": "False", "message": message}]}
    assert _pending_code(status, "nvidia.com/gpu") == code


@pytest.mark.asyncio
async def test_log_marker_is_cached_by_pod_uid_and_native_pods_are_not_read(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("test-token-not-a-real-credential")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/namespaces/science/pods/stage-1/log"
        assert request.url.params["container"] == "scientific-stage"
        assert request.url.params["timestamps"] == "true"
        marker = {"event": "scientific_snapshot_request", "mechanism": "cuda-criu-restored"}
        return httpx.Response(200, text=f'{at(7).isoformat()} {json.dumps(marker)}')

    async with httpx.AsyncClient(base_url="https://cluster.invalid", transport=httpx.MockTransport(handler)) as client:
        cluster = HttpScientificBatchCluster(
            base_url="https://cluster.invalid", ca_file=tmp_path / "ca", token_file=token,
            controller_id=str(uuid4()), fence=Mock(), renderer=Mock(), writes_enabled=False, client=client,
        )
        assert await cluster._snapshot_request_at(pod(snapshot=False), "science") is None
        assert await cluster._snapshot_request_at(pod(), "science") == at(7)
        assert await cluster._snapshot_request_at(pod(), "science") == at(7)
        assert len(requests) == 1
