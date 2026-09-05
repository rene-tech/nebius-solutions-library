from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from fs2_serve.admission import AdmissionService
from fs2_serve.gpu_allocation_observer import (
    SCIENTIFIC_MODEL_ID_LABEL,
    KubernetesGpuAllocationPublisher,
    parse_kubelet_device_checkpoint,
)
from fs2_serve.lifecycle import (
    LifecycleSubject,
    MeasurementQuality,
    MemoryLifecycleRepository,
    ReproducibilityMetadata,
    WorkloadTelemetryKind,
)
from fs2_serve.model_deployment import MODEL_ID_LABEL
from fs2_serve.models import (
    ClaimedOperation,
    OperationStatus,
    RuntimeIdentity,
    RuntimeLifecycleObservation,
    RuntimeObservationSource,
    RuntimeObservedPhase,
    RuntimePhaseObservation,
)
from fs2_serve.runtime_kubernetes import (
    GPU_ALLOCATION_OBSERVED_AT_ANNOTATION,
    GPU_OBSERVER_RESOLUTION_ANNOTATION,
    GPU_UUIDS_ANNOTATION,
    KubernetesRuntimeMetadataProvider,
)

NOW = datetime(2026, 9, 4, 17, 39, tzinfo=UTC)
GPU_UUID = "GPU-29f2b6df-1bed-2192-f0a0-e60439b77c8d"
EVIDENCE = (
    Path(__file__).resolve().parents[3] / "catalog/profiles/evidence/h100-qwen-cosmos-live-benchmark-20260904.json"
)


def _iso(offset: float) -> str:
    return (NOW + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def _pod(
    model_id: str,
    *,
    uid: str = "pod-uid-1",
    annotations: Mapping[str, str] | None = None,
    model_label: str = MODEL_ID_LABEL,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"{model_id}-runtime",
            "namespace": "fs2-models",
            "uid": uid,
            "resourceVersion": "42",
            "creationTimestamp": _iso(1),
            "labels": {model_label: model_id},
            "annotations": dict(annotations or {}),
        },
        "spec": {
            "nodeName": "gpu-node-1",
            "containers": [
                {
                    "name": "runtime",
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    },
                }
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {"type": "PodScheduled", "status": "True", "lastTransitionTime": _iso(10)},
                {"type": "Ready", "status": "True", "lastTransitionTime": _iso(100)},
            ],
            "containerStatuses": [{"name": "runtime", "state": {"running": {"startedAt": _iso(20)}}}],
        },
    }


class FakeReader:
    def __init__(self, *, pods: list[dict[str, Any]], events: list[dict[str, Any]] | None = None) -> None:
        self.pods = pods
        self.events = events or []

    async def list(self, path: str) -> list[Mapping[str, Any]]:
        if path.endswith("/pods"):
            return self.pods
        if path.endswith("/events"):
            return self.events
        raise AssertionError(path)

    async def get(self, path: str) -> Mapping[str, Any]:
        assert path == "/api/v1/nodes/gpu-node-1"
        return {
            "metadata": {
                "name": "gpu-node-1",
                "uid": "node-uid-1",
                "labels": {"capacity.fs2.nebius/type": "preemptible"},
            }
        }


def _benchmark_model_ids() -> tuple[str, ...]:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert payload["gpu_lifecycle_accounting"]["qwen3-8b"]["phase_values_gpu_seconds"] == 0
    assert payload["gpu_lifecycle_accounting"]["cosmos3-nano"]["quality"] == "unavailable"
    return tuple(item["model_id"] for item in payload["models"])


@pytest.mark.parametrize("model_id", _benchmark_model_ids())
@pytest.mark.asyncio
async def test_benchmark_models_resolve_exact_kubernetes_and_kubelet_identity(model_id: str) -> None:
    annotations = {
        GPU_UUIDS_ANNOTATION: json.dumps([GPU_UUID]),
        GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: _iso(12),
        GPU_OBSERVER_RESOLUTION_ANNOTATION: "1",
        "telemetry.fs2.nebius.ai/phase-artifact_load-started-at": _iso(20),
        "telemetry.fs2.nebius.ai/phase-artifact_load-completed-at": _iso(90),
    }
    events = [
        {
            "metadata": {"uid": "event-pulling"},
            "reason": "Pulling",
            "eventTime": _iso(12),
            "involvedObject": {"uid": "pod-uid-1", "fieldPath": "spec.containers{runtime}"},
        },
        {
            "metadata": {"uid": "event-pulled"},
            "reason": "Pulled",
            "eventTime": _iso(20),
            "involvedObject": {"uid": "pod-uid-1", "fieldPath": "spec.containers{runtime}"},
        },
    ]
    provider = KubernetesRuntimeMetadataProvider(
        FakeReader(pods=[_pod(model_id, annotations=annotations)], events=events)
    )

    observation = await provider.resolve_lifecycle(operation_id=uuid4(), model_id=model_id)

    assert observation is not None
    assert observation.runtime == RuntimeIdentity(
        pod_uid="pod-uid-1",
        node_uid="node-uid-1",
        gpu_uuids=[GPU_UUID],
        gpu_count=1,
        preemptible=True,
    )
    assert observation.pod_scheduled_at == NOW + timedelta(seconds=10)
    assert {(phase.phase, phase.started_at, phase.completed_at) for phase in observation.phases} == {
        (RuntimeObservedPhase.IMAGE_PULL, NOW + timedelta(seconds=12), NOW + timedelta(seconds=20)),
        (RuntimeObservedPhase.ARTIFACT_LOAD, NOW + timedelta(seconds=20), NOW + timedelta(seconds=90)),
    }


@pytest.mark.asyncio
async def test_runtime_attribution_fails_closed_when_multiple_ready_replicas_are_ambiguous() -> None:
    provider = KubernetesRuntimeMetadataProvider(
        FakeReader(pods=[_pod("qwen3-8b", uid="pod-a"), _pod("qwen3-8b", uid="pod-b")])
    )
    assert await provider.resolve_lifecycle(operation_id=uuid4(), model_id="qwen3-8b") is None
    assert await provider.resolve(operation_id=uuid4(), model_id="qwen3-8b") == RuntimeIdentity()


def test_kubelet_checkpoint_parser_extracts_only_exact_nvidia_allocations() -> None:
    checkpoint = {
        "Data": {
            "PodDeviceEntries": [
                {
                    "PodUID": "pod-uid-1",
                    "ContainerName": "runtime",
                    "ResourceName": "nvidia.com/gpu",
                    "DeviceIDs": {"0": [GPU_UUID]},
                    "AllocResp": "deliberately-ignored",
                },
                {
                    "PodUID": "cpu-pod",
                    "ContainerName": "runtime",
                    "ResourceName": "example.com/not-a-gpu",
                    "DeviceIDs": {"0": ["secret-ish-device"]},
                },
            ]
        },
        "Checksum": 123,
    }
    assert parse_kubelet_device_checkpoint(json.dumps(checkpoint).encode()) == {"pod-uid-1": (GPU_UUID,)}


@pytest.mark.asyncio
async def test_gpu_observer_publishes_first_observation_without_overwriting(tmp_path: Path) -> None:
    patches: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": [_pod("qwen3-8b")]})
        assert request.method == "PATCH"
        patches.append(json.loads(request.content))
        return httpx.Response(200, json={})

    token_file = tmp_path / "token"
    token_file.write_text("t" * 32, encoding="utf-8")
    publisher = KubernetesGpuAllocationPublisher(
        base_url="https://kubernetes.default.svc",
        token_file=token_file,
        ca_file=Path("/unused"),
        namespace="fs2-models",
        node_name="gpu-node-1",
        poll_seconds=1,
    )
    async with httpx.AsyncClient(
        base_url=publisher.base_url,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        published = await publisher.publish_once(
            client,
            {"pod-uid-1": (GPU_UUID,)},
            observed_at=NOW + timedelta(seconds=12),
        )
    assert published == 1
    annotations = patches[0]["metadata"]["annotations"]
    assert annotations == {
        GPU_UUIDS_ANNOTATION: json.dumps([GPU_UUID], separators=(",", ":")),
        GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: _iso(12),
        GPU_OBSERVER_RESOLUTION_ANNOTATION: "1",
    }


@pytest.mark.asyncio
async def test_gpu_observer_publishes_scientific_pod_allocation(tmp_path: Path) -> None:
    patches: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"items": [_pod("rfdiffusion", model_label=SCIENTIFIC_MODEL_ID_LABEL)]},
            )
        assert request.method == "PATCH"
        patches.append(json.loads(request.content))
        return httpx.Response(200, json={})

    token_file = tmp_path / "token"
    token_file.write_text("t" * 32, encoding="utf-8")
    publisher = KubernetesGpuAllocationPublisher(
        base_url="https://kubernetes.default.svc",
        token_file=token_file,
        ca_file=Path("/unused"),
        namespace="fs2-models",
        node_name="gpu-node-1",
        poll_seconds=1,
    )
    async with httpx.AsyncClient(
        base_url=publisher.base_url,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        published = await publisher.publish_once(
            client,
            {"pod-uid-1": (GPU_UUID,)},
            observed_at=NOW + timedelta(seconds=12),
        )

    assert published == 1
    assert len(patches) == 1


@pytest.mark.asyncio
async def test_gpu_observer_rejects_conflicting_model_labels(tmp_path: Path) -> None:
    pod = _pod("rfdiffusion", model_label=SCIENTIFIC_MODEL_ID_LABEL)
    pod["metadata"]["labels"][MODEL_ID_LABEL] = "different-model"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"items": [pod]})

    token_file = tmp_path / "token"
    token_file.write_text("t" * 32, encoding="utf-8")
    publisher = KubernetesGpuAllocationPublisher(
        base_url="https://kubernetes.default.svc",
        token_file=token_file,
        ca_file=Path("/unused"),
        namespace="fs2-models",
        node_name="gpu-node-1",
        poll_seconds=1,
    )
    async with httpx.AsyncClient(
        base_url=publisher.base_url,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        published = await publisher.publish_once(
            client,
            {"pod-uid-1": (GPU_UUID,)},
            observed_at=NOW + timedelta(seconds=12),
        )

    assert published == 0


def _claimed(operation_id: UUID) -> ClaimedOperation:
    return ClaimedOperation(
        id=operation_id,
        tenant_id="tenant-a",
        principal_id="principal-a",
        token_id=uuid4(),
        model_id="qwen3-8b",
        model_revision="dynamic:sha256:" + "a" * 64,
        protocol="openai-chat",
        operation="chat",
        idempotency_key="lifecycle-attribution-0001",
        status=OperationStatus.RUNNING,
        accepted_at=NOW,
        available_at=NOW,
        activation_started_at=NOW,
        ready_at=NOW + timedelta(seconds=100),
        started_at=NOW + timedelta(seconds=102),
        deadline_at=NOW + timedelta(minutes=10),
        attempt=1,
        max_attempts=2,
        fencing_token=1,
        request_content_type="application/json",
        worker_id="worker-a",
    )


@pytest.mark.asyncio
async def test_online_lifecycle_records_measured_occupied_idle_and_reconciles() -> None:
    operation_id = uuid4()
    claimed = _claimed(operation_id)
    repository = MemoryLifecycleRepository()
    await repository.register_subject(
        LifecycleSubject(
            subject_id=operation_id,
            workload_kind=WorkloadTelemetryKind.ONLINE,
            operation_id=operation_id,
            request_id=operation_id,
            workload_id=operation_id,
            tenant_id=claimed.tenant_id,
            principal_id=claimed.principal_id,
            model_id=claimed.model_id,
            model_revision=claimed.model_revision,
            protocol=claimed.protocol,
            trace_id="1" * 32,
            parent_span_id="2" * 16,
            accepted_at=claimed.accepted_at,
            reproducibility=ReproducibilityMetadata(),
        )
    )
    service = AdmissionService(
        registry=cast(Any, object()),
        store=cast(Any, object()),
        runtime=cast(Any, object()),
        metrics=cast(Any, object()),
        worker_concurrency=1,
        poll_seconds=1,
        lease_seconds=30,
        maintenance_interval_seconds=30,
        shutdown_grace_seconds=30,
        lifecycle=repository,
    )
    runtime = RuntimeIdentity(
        pod_uid="pod-uid-1",
        node_uid="node-uid-1",
        gpu_uuids=[GPU_UUID],
        gpu_count=1,
        preemptible=True,
    )
    observation = RuntimeLifecycleObservation(
        runtime=runtime,
        namespace="fs2-models",
        pod_name="qwen3-8b-runtime",
        node_name="gpu-node-1",
        pod_created_at=NOW + timedelta(seconds=1),
        pod_scheduled_at=NOW + timedelta(seconds=10),
        container_started_at=NOW + timedelta(seconds=20),
        ready_at=NOW + timedelta(seconds=100),
        device_allocation_observed_at=NOW + timedelta(seconds=12),
        device_observation_resolution_seconds=1,
        phases=[
            RuntimePhaseObservation(
                phase=RuntimeObservedPhase.IMAGE_PULL,
                started_at=NOW + timedelta(seconds=12),
                completed_at=NOW + timedelta(seconds=20),
                source=RuntimeObservationSource.KUBERNETES,
            ),
            RuntimePhaseObservation(
                phase=RuntimeObservedPhase.ARTIFACT_LOAD,
                started_at=NOW + timedelta(seconds=20),
                completed_at=NOW + timedelta(seconds=90),
                source=RuntimeObservationSource.CONTROLLER,
            ),
            RuntimePhaseObservation(
                phase=RuntimeObservedPhase.COMPILE,
                started_at=NOW + timedelta(seconds=90),
                completed_at=NOW + timedelta(seconds=98),
                source=RuntimeObservationSource.CONTROLLER,
            ),
            RuntimePhaseObservation(
                phase=RuntimeObservedPhase.WARMUP,
                started_at=NOW + timedelta(seconds=98),
                completed_at=NOW + timedelta(seconds=100),
                source=RuntimeObservationSource.CONTROLLER,
            ),
        ],
    )

    await service._record_runtime_observation(
        claimed,
        runtime,
        observation=observation,
        started_at=NOW + timedelta(seconds=102),
        completed_at=NOW + timedelta(seconds=105),
    )
    await service._record_release(claimed, NOW + timedelta(seconds=105))
    rollup = await repository.reconcile(operation_id, terminal=True, outcome="succeeded")

    assert rollup is not None
    assert rollup.scheduler_occupied_gpu_seconds == pytest.approx(95)
    assert rollup.device_allocated_gpu_seconds == pytest.approx(93)
    assert rollup.active_gpu_seconds == pytest.approx(3)
    assert rollup.occupied_idle_gpu_seconds == pytest.approx(92)
    assert rollup.phase_gpu_seconds["image_pull"] == pytest.approx(8)
    assert rollup.phase_gpu_seconds["artifact_load"] == pytest.approx(70)
    assert rollup.phase_gpu_seconds["resident_idle"] == pytest.approx(2)
    assert rollup.phase_gpu_seconds["cooldown_grace"] == 0
    assert rollup.phase_gpu_seconds["teardown"] == 0
    assert rollup.reconciliation_delta_seconds == 0
    assert rollup.device_scheduler_delta_seconds == pytest.approx(2)
    assert rollup.quality is MeasurementQuality.APPLICATION_OBSERVED
    assert rollup.reconciled is True
    assert "release_event_missing" not in rollup.data_gaps
