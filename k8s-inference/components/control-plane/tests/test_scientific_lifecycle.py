from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fs2_serve.lifecycle import LifecyclePhase, LifecycleSource, MemoryLifecycleRepository
from fs2_serve.models import OperationStatus, OperationView
from fs2_serve.scientific_lifecycle import ScientificResultLifecycleProjector
from fs2_serve.scientific_run_result import ScientificRunResult

FIXTURE = Path(__file__).parent / "fixtures" / "scientific-lifecycle-result-v1.json"


def canonical_result() -> ScientificRunResult:
    return ScientificRunResult.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def operation() -> OperationView:
    result = canonical_result()
    return OperationView(
        id=UUID(result.operation_id),
        tenant_id="tenant-a",
        principal_id="scientist-a",
        token_id=UUID("55555555-5555-4555-8555-555555555555"),
        model_id=result.execution_identity.model_id,
        model_revision=result.execution_identity.model_revision,
        protocol="scientific-batch-v1",
        operation="design",
        idempotency_key="scientific-lifecycle-projection-0001",
        status=OperationStatus.SUCCEEDED,
        accepted_at=result.submitted_at,
        available_at=result.submitted_at,
        completed_at=result.completed_at,
        outcome="succeeded",
        semantic_outcome="passed",
        result_available=True,
    )


@pytest.mark.asyncio
async def test_canonical_result_projects_actual_kueue_and_service_class_without_duration_guessing() -> None:
    repository = MemoryLifecycleRepository()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    projector = ScientificResultLifecycleProjector(
        repository,
        cluster="k8s-inference-h100",
        tracer=provider.get_tracer("scientific-lifecycle-test"),
    )
    result = canonical_result()
    current = operation()

    first = await projector.project(current, result)
    second = await projector.project(current, result)
    provider.shutdown()

    assert first == second
    assert len(first) == 1
    detail = await repository.get_workload(UUID(result.attempts[0].attempt_id), tenant_id="tenant-a")
    assert detail is not None
    assert detail.subject.operation_id == current.id
    assert detail.subject.batch_id == UUID(result.batch_id)
    assert detail.subject.workload_id == UUID(result.workload_id)
    assert detail.subject.api_key_fingerprint is not None
    admission = next(signal for signal in detail.signals if signal.phase is LifecyclePhase.ADMIT)
    assert admission.source is LifecycleSource.KUEUE
    assert admission.queue_name == "tenant-a-protein-design"
    assert admission.kueue_workload_uid == "kueue-uid-1"
    assert admission.gpu_count == 1
    assert admission.detail == {
        "accelerator_count": "1",
        "accelerator_resource_name": "nvidia.com/gpu",
        "cluster_queue": "inference-accelerators",
        "local_queue": "tenant-a-protein-design",
        "resolved_pool_id": "h100-capacity-block",
        "resource_flavor": "h100-capacity-block",
        "result_digest": result.digest,
        "service_class": "customer-batch",
    }
    assert len(detail.correlations) == 4
    gpu = next(value for value in detail.correlations if value.gpu_uuid is not None)
    assert (gpu.pod_uid, gpu.gpu_uuid, gpu.gpu_rank) == (
        "pod-uid-1",
        "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        0,
    )
    assert detail.rollup is not None
    assert detail.rollup.scheduler_occupied_gpu_seconds == 0
    assert detail.rollup.device_allocated_gpu_seconds == 0
    assert detail.rollup.reconciled is False
    assert "scheduler_occupancy_clock_missing" in detail.rollup.data_gaps
    assert "device_allocation_clock_missing" in detail.rollup.data_gaps
    assert "release_event_missing" in detail.rollup.data_gaps

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "fs2.scientific.lifecycle.project")
    attempt = next(span for span in spans if span.name == "fs2.scientific.lifecycle.attempt")
    assert root.attributes is not None
    assert root.attributes["fs2.service_class"] == "customer-batch"
    assert root.attributes["fs2.result.digest"] == result.digest
    assert attempt.attributes is not None
    assert attempt.attributes["fs2.kueue.pool"] == "h100-capacity-block"
    assert attempt.attributes["fs2.kueue.resource_flavor"] == "h100-capacity-block"
    encoded = json.dumps([dict(span.attributes or {}) for span in spans], sort_keys=True)
    assert "request_body" not in encoded
    assert "authorization" not in encoded


@pytest.mark.asyncio
async def test_multi_pod_result_does_not_invent_a_pod_gpu_join() -> None:
    repository = MemoryLifecycleRepository()
    result = canonical_result()
    attempt = result.attempts[0].model_copy(
        update={
            "pod_uids": ("pod-uid-1", "pod-uid-2"),
            "gpu_uuids": (
                "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "GPU-ffffffff-1111-2222-3333-444444444444",
            ),
        }
    )
    result = result.model_copy(update={"attempts": (attempt,)})

    await ScientificResultLifecycleProjector(repository).project(operation(), result)
    detail = await repository.get_workload(UUID(attempt.attempt_id), tenant_id="tenant-a")

    assert detail is not None
    assert {item.pod_uid for item in detail.correlations if item.pod_uid is not None} == {
        "pod-uid-1",
        "pod-uid-2",
    }
    assert not any(item.gpu_uuid is not None for item in detail.correlations)


@pytest.mark.asyncio
async def test_projection_fails_closed_when_result_differs_from_durable_operation() -> None:
    current = operation().model_copy(update={"model_revision": "b" * 40})
    with pytest.raises(ValueError, match="durable operation identity"):
        await ScientificResultLifecycleProjector(MemoryLifecycleRepository()).project(
            current,
            canonical_result(),
        )


def test_fixture_contains_no_payload_or_secret_fields() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    encoded = json.dumps(document, sort_keys=True)
    for forbidden in ("authorization", "cookie", "credential", "request_body", "sequence"):
        assert forbidden not in encoded.lower()
    assert datetime.fromisoformat(document["submitted_at"]).tzinfo is UTC
