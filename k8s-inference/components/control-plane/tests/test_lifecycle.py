from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fs2_serve.lifecycle import (
    ArtifactReference,
    LifecycleClock,
    LifecycleCorrelation,
    LifecycleEdge,
    LifecyclePhase,
    LifecycleSignal,
    LifecycleSource,
    LifecycleSubject,
    MeasurementQuality,
    MemoryLifecycleRepository,
    ReproducibilityMetadata,
    WorkloadTelemetryKind,
    api_key_id_hash,
    reconcile_lifecycle,
    reproducibility_metadata,
    trace_identity,
)

NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
GPU_A = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GPU_B = "GPU-bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def subject(*, tenant_id: str = "oncology-a") -> LifecycleSubject:
    operation_id = uuid4()
    token_id = uuid4()
    return LifecycleSubject(
        subject_id=operation_id,
        workload_kind=WorkloadTelemetryKind.ONLINE,
        operation_id=operation_id,
        request_id=operation_id,
        workload_id=operation_id,
        tenant_id=tenant_id,
        principal_id="operator@example.test",
        api_key_id=token_id,
        api_key_fingerprint=api_key_id_hash(token_id),
        model_id="rfdiffusion",
        model_revision="sha256:" + "a" * 64,
        protocol="scientific-batch",
        trace_id="1" * 32,
        parent_span_id="2" * 16,
        accepted_at=NOW,
        reproducibility=ReproducibilityMetadata(
            input_shape={"type": "object", "field_count": 2},
            parameter_digest="sha256:" + "b" * 64,
            artifact_references=[
                ArtifactReference(
                    role="target_structure",
                    uri="s3://fs2-artifacts/sha256/" + "c" * 64,
                    digest="sha256:" + "c" * 64,
                    size_bytes=123,
                )
            ],
        ),
    )


def interval(
    current: LifecycleSubject,
    *,
    key: str,
    start: float,
    end: float,
    clock: LifecycleClock,
    phase: LifecyclePhase,
    gpu_count: int = 0,
    pod_uid: str | None = None,
    kueue_uid: str | None = None,
    gpu_uuid: str | None = None,
    gpu_rank: int | None = None,
    resolution: float = 0,
) -> list[LifecycleSignal]:
    return [
        LifecycleSignal(
            event_key=f"{key}:{edge.value}",
            subject_id=current.subject_id,
            occurred_at=NOW + timedelta(seconds=offset),
            observed_at=NOW + timedelta(seconds=end),
            source=LifecycleSource.KUBERNETES,
            source_resolution_seconds=resolution,
            quality=MeasurementQuality.MEASURED,
            phase=phase,
            edge=edge,
            clock=clock,
            interval_key=key,
            gpu_count=gpu_count,
            pod_uid=pod_uid,
            kueue_workload_uid=kueue_uid,
            gpu_uuid=gpu_uuid,
            gpu_rank=gpu_rank,
        )
        for edge, offset in ((LifecycleEdge.START, start), (LifecycleEdge.END, end))
    ]


def test_reproducibility_records_shapes_and_digests_without_payload_values_or_field_names() -> None:
    secret = "RAW_SEQUENCE_AND_TOKEN_MUST_NOT_APPEAR"
    body = json.dumps(
        {
            secret: ["MKTIIALSYIFCLVFA", "GGH"],
            "parameters": {"seed": 17, "temperature": 0.4},
            "artifacts": [
                {
                    "role": "target_structure",
                    "uri": "https://artifacts.example.test/sha256/" + "d" * 64,
                    "digest": "sha256:" + "d" * 64,
                    "size_bytes": 42,
                    "ignored": secret,
                }
            ],
        }
    ).encode()

    metadata = reproducibility_metadata(body, "application/json")
    encoded = metadata.model_dump_json()

    assert secret not in encoded
    assert "MKTIIALSYIFCLVFA" not in encoded
    assert "temperature" not in encoded
    assert metadata.parameter_digest is not None
    assert metadata.artifact_references[0].uri.endswith("d" * 64)
    assert metadata.input_shape["bytes"] == len(body)


def test_secret_bearing_artifact_uris_and_arbitrary_event_detail_are_rejected() -> None:
    with pytest.raises(ValidationError, match="non-approved field"):
        ReproducibilityMetadata(input_shape={"raw_sequence": "MKTIIALSYIFCLVFA"})
    with pytest.raises(ValidationError, match="must not contain credentials"):
        ArtifactReference(
            role="weights",
            uri="https://user:password@example.test/model?signature=secret",
            digest="sha256:" + "a" * 64,
        )
    with pytest.raises(ValidationError, match="non-approved field"):
        LifecycleSignal(
            event_key="unsafe-detail",
            subject_id=uuid4(),
            occurred_at=NOW,
            observed_at=NOW,
            source=LifecycleSource.APPLICATION,
            quality=MeasurementQuality.APPLICATION_OBSERVED,
            phase=LifecyclePhase.RECEIVE,
            edge=LifecycleEdge.INSTANT,
            clock=LifecycleClock.LIFECYCLE,
            detail={"raw_payload": "secret"},
        )


def test_allocation_facts_require_exact_kueue_pod_and_gpu_coordinates() -> None:
    current = subject()
    with pytest.raises(ValidationError, match="Kueue Workload UID"):
        interval(
            current,
            key="quota-without-workload",
            start=0,
            end=1,
            clock=LifecycleClock.QUOTA_RESERVED,
            phase=LifecyclePhase.ADMIT,
            gpu_count=1,
        )
    with pytest.raises(ValidationError, match="Pod UID"):
        interval(
            current,
            key="scheduler-without-pod",
            start=0,
            end=1,
            clock=LifecycleClock.SCHEDULER_OCCUPIED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=1,
        )
    with pytest.raises(ValidationError, match="Pod UID"):
        LifecycleCorrelation(
            correlation_key="gpu-without-pod",
            subject_id=current.subject_id,
            observed_at=NOW,
            source=LifecycleSource.DCGM,
            gpu_uuid=GPU_A,
            gpu_rank=0,
        )


def test_three_clocks_and_overlapping_phases_reconcile_without_rank_double_counting() -> None:
    current = subject()
    signals = [
        *interval(
            current,
            key="quota",
            start=0,
            end=110,
            clock=LifecycleClock.QUOTA_RESERVED,
            phase=LifecyclePhase.ADMIT,
            gpu_count=2,
            kueue_uid="kueue-uid-1",
            resolution=5,
        ),
        *interval(
            current,
            key="scheduled-a",
            start=0,
            end=100,
            clock=LifecycleClock.SCHEDULER_OCCUPIED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=2,
            pod_uid="pod-uid-1",
            resolution=5,
        ),
        # Duplicate collector evidence for the same Pod ranks must merge.
        *interval(
            current,
            key="scheduled-b",
            start=0,
            end=100,
            clock=LifecycleClock.SCHEDULER_OCCUPIED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=2,
            pod_uid="pod-uid-1",
            resolution=5,
        ),
        *interval(
            current,
            key="device-a",
            start=5,
            end=95,
            clock=LifecycleClock.DEVICE_ALLOCATED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=1,
            pod_uid="pod-uid-1",
            gpu_uuid=GPU_A,
            gpu_rank=0,
            resolution=5,
        ),
        # Overlapping kubelet/DCGM evidence for one physical device is one lane.
        *interval(
            current,
            key="device-a-copy",
            start=5,
            end=95,
            clock=LifecycleClock.DEVICE_ALLOCATED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=1,
            pod_uid="pod-uid-1",
            gpu_uuid=GPU_A,
            gpu_rank=0,
            resolution=5,
        ),
        *interval(
            current,
            key="device-b",
            start=5,
            end=95,
            clock=LifecycleClock.DEVICE_ALLOCATED,
            phase=LifecyclePhase.GPU_ALLOCATION,
            gpu_count=1,
            pod_uid="pod-uid-1",
            gpu_uuid=GPU_B,
            gpu_rank=1,
            resolution=5,
        ),
        *interval(
            current,
            key="image",
            start=0,
            end=20,
            clock=LifecycleClock.PHASE,
            phase=LifecyclePhase.IMAGE_PULL,
            pod_uid="pod-uid-1",
        ),
        *interval(
            current,
            key="artifact",
            start=15,
            end=30,
            clock=LifecycleClock.PHASE,
            phase=LifecyclePhase.ARTIFACT_LOAD,
            pod_uid="pod-uid-1",
        ),
        *interval(
            current,
            key="active",
            start=30,
            end=80,
            clock=LifecycleClock.PHASE,
            phase=LifecyclePhase.ACTIVE_COMPUTE,
            pod_uid="pod-uid-1",
        ),
        *interval(
            current,
            key="grace",
            start=80,
            end=100,
            clock=LifecycleClock.PHASE,
            phase=LifecyclePhase.COOLDOWN_GRACE,
            pod_uid="pod-uid-1",
        ),
        LifecycleSignal(
            event_key="release",
            subject_id=current.subject_id,
            occurred_at=NOW + timedelta(seconds=100),
            observed_at=NOW + timedelta(seconds=100),
            source=LifecycleSource.KUBERNETES,
            quality=MeasurementQuality.MEASURED,
            phase=LifecyclePhase.RELEASE,
            edge=LifecycleEdge.INSTANT,
            clock=LifecycleClock.LIFECYCLE,
        ),
    ]
    signals = [value.model_copy(update={"sequence": index}) for index, value in enumerate(signals, start=1)]

    incomplete = reconcile_lifecycle(current, signals[:-1], terminal=True, outcome="succeeded", generated_at=NOW)
    assert incomplete.reconciled is False
    assert "release_event_missing" in incomplete.data_gaps

    rollup = reconcile_lifecycle(current, signals, terminal=True, outcome="succeeded", generated_at=NOW)

    assert rollup.quota_reserved_gpu_seconds == 220
    assert rollup.scheduler_occupied_gpu_seconds == 200
    assert rollup.device_allocated_gpu_seconds == 180
    assert rollup.active_gpu_seconds == 100
    assert rollup.occupied_idle_gpu_seconds == 100
    assert rollup.phase_gpu_seconds["image_pull"] == 30
    assert rollup.phase_gpu_seconds["artifact_load"] == 30
    assert rollup.phase_gpu_seconds["cooldown_grace"] == 40
    assert rollup.reconciliation_delta_seconds == 0
    assert rollup.device_scheduler_delta_seconds == 20
    assert rollup.tolerance_seconds == 20
    assert rollup.reconciled
    assert rollup.data_gaps == []


@pytest.mark.asyncio
async def test_memory_repository_is_append_only_idempotent_and_tenant_scoped() -> None:
    repository = MemoryLifecycleRepository()
    current = subject()
    other = subject(tenant_id="oncology-b")
    await repository.register_subject(current)
    await repository.register_subject(current)
    await repository.register_subject(other)
    correlation = LifecycleCorrelation(
        correlation_key="pod-binding",
        subject_id=current.subject_id,
        observed_at=NOW,
        source=LifecycleSource.KUBERNETES,
        pod_uid="pod-uid-1",
        node_uid="node-uid-1",
        gpu_uuid=GPU_A,
        gpu_rank=0,
    )
    await repository.append_correlations([correlation])
    await repository.append_correlations([correlation])
    receive = LifecycleSignal(
        event_key="receive",
        subject_id=current.subject_id,
        occurred_at=NOW,
        observed_at=NOW,
        source=LifecycleSource.APPLICATION,
        quality=MeasurementQuality.APPLICATION_OBSERVED,
        phase=LifecyclePhase.RECEIVE,
        edge=LifecycleEdge.INSTANT,
        clock=LifecycleClock.LIFECYCLE,
    )
    await repository.append_signals([receive])
    await repository.append_signals([receive])

    listing = await repository.list_workloads(
        tenant_id="oncology-a",
        model_id=None,
        operation_id=None,
        limit=100,
    )
    detail = await repository.get_workload(current.subject_id, tenant_id="oncology-a")

    assert listing.total == 1
    assert detail is not None
    assert detail.payloads_exposed is False
    assert len(detail.correlations) == 1
    assert len(detail.signals) == 1
    assert await repository.get_workload(current.subject_id, tenant_id="oncology-b") is None
    with pytest.raises(ValueError, match="different facts"):
        await repository.append_correlations([correlation.model_copy(update={"node_uid": "different-node"})])


def test_trace_context_and_migration_contract_are_strict() -> None:
    assert trace_identity("00-" + "1" * 32 + "-" + "2" * 16 + "-01") == ("1" * 32, "2" * 16)
    assert trace_identity("00-" + "0" * 32 + "-" + "2" * 16 + "-01") == (None, None)
    migration = (
        Path(__file__).resolve().parents[1] / "migrations" / "0018_workload_lifecycle_telemetry.sql"
    ).read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE" in migration
    assert "fs2_reporting_gpu_phase_usage" in migration
    assert "NULLS NOT DISTINCT" in migration
    assert "raw_payload" not in migration
