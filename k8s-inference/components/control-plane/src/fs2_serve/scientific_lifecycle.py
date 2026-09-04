"""Canonical scientific-result projection into the durable lifecycle ledger.

The terminal artifact result is the public authority for the frozen service
class and the Kueue admission which actually happened.  This adapter consumes
that typed object directly; it does not define a second scheduling transport or
infer allocation intervals from run latency.  Controller, Kubernetes, kubelet,
and DCGM observers remain responsible for timestamped occupancy edges.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, TypedDict
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Tracer
from pydantic import JsonValue

from .lifecycle import (
    LifecycleClock,
    LifecycleCorrelation,
    LifecycleEdge,
    LifecyclePhase,
    LifecycleRepository,
    LifecycleRollup,
    LifecycleSignal,
    LifecycleSource,
    LifecycleSubject,
    MeasurementQuality,
    ReproducibilityMetadata,
    WorkloadTelemetryKind,
    api_key_id_hash,
)
from .models import OperationView
from .scientific_run_result import ResultAttempt, ScientificRunResult, StageSchedulingDecision


class _CorrelationCommon(TypedDict):
    subject_id: UUID
    observed_at: datetime
    attempt: int
    cluster: str | None
    namespace: str
    queue_name: str
    kueue_workload_uid: str | None
    job_uid: str | None


class _SignalCommon(_CorrelationCommon):
    source_resolution_seconds: float
    quality: MeasurementQuality
    edge: LifecycleEdge
    clock: LifecycleClock


class ScientificResultLifecycleSink(Protocol):
    """Behavioral seam whose payload is the canonical result itself."""

    async def project(
        self,
        operation: OperationView,
        result: ScientificRunResult,
        *,
        reproducibility: ReproducibilityMetadata | None = None,
    ) -> tuple[LifecycleRollup, ...]: ...


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"canonical scientific result {label} is not a UUID") from None


def _stage_map(result: ScientificRunResult) -> Mapping[str, StageSchedulingDecision]:
    return {stage.stage_id: stage for stage in result.scheduling_snapshot.stages}


class ScientificResultLifecycleProjector:
    """Idempotently bind terminal scientific identity to lifecycle subjects."""

    def __init__(
        self,
        lifecycle: LifecycleRepository,
        *,
        cluster: str | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        if cluster is not None and (not cluster or len(cluster) > 128):
            raise ValueError("scientific lifecycle cluster identity is invalid")
        self.lifecycle = lifecycle
        self.cluster = cluster
        self._tracer = tracer or trace.get_tracer("fs2_serve.scientific_lifecycle")

    @staticmethod
    def _validate_identity(operation: OperationView, result: ScientificRunResult) -> tuple[UUID, UUID, UUID]:
        operation_id = _uuid(result.operation_id, "operation_id")
        batch_id = _uuid(result.batch_id, "batch_id")
        workload_id = _uuid(result.workload_id, "workload_id")
        if operation.protocol != "scientific-batch-v1":
            raise ValueError("canonical scientific result requires a scientific-batch operation")
        if (
            operation.id != operation_id
            or operation.model_id != result.execution_identity.model_id
            or operation.model_revision != result.execution_identity.model_revision
            or operation.accepted_at != result.submitted_at
        ):
            raise ValueError("canonical scientific result differs from its durable operation identity")
        return operation_id, batch_id, workload_id

    @staticmethod
    def _admission_detail(
        result: ScientificRunResult,
        stage: StageSchedulingDecision,
        attempt: ResultAttempt,
    ) -> dict[str, JsonValue]:
        detail: dict[str, JsonValue] = {
            "cluster_queue": stage.resolved_cluster_queue,
            "local_queue": stage.resolved_local_queue,
            "result_digest": result.digest,
            "service_class": result.scheduling_snapshot.service_class.value,
        }
        admission = attempt.scheduling_admission
        if admission is None:
            return detail
        detail["accelerator_count"] = str(admission.accelerator_count)
        if admission.resolved_pool_id is not None:
            detail["resolved_pool_id"] = admission.resolved_pool_id
        if admission.admitted_resource_flavor is not None:
            detail["resource_flavor"] = admission.admitted_resource_flavor
        if admission.accelerator_resource_name is not None:
            detail["accelerator_resource_name"] = admission.accelerator_resource_name
        return detail

    def _correlations(
        self,
        result: ScientificRunResult,
        attempt: ResultAttempt,
        stage: StageSchedulingDecision,
    ) -> list[LifecycleCorrelation]:
        subject_id = _uuid(attempt.attempt_id, "attempt_id")
        prefix = f"scientific-result:{subject_id}"
        observed_at = result.completed_at
        common: _CorrelationCommon = {
            "subject_id": subject_id,
            "observed_at": observed_at,
            "attempt": attempt.attempt_number,
            "cluster": self.cluster,
            "namespace": result.scheduling_snapshot.workload_namespace,
            "queue_name": stage.resolved_local_queue,
            "kueue_workload_uid": attempt.kueue_workload_uid,
            "job_uid": attempt.k8s_job_uid,
        }
        correlations = [
            LifecycleCorrelation(
                correlation_key=f"{prefix}:admission",
                source=(
                    LifecycleSource.KUEUE
                    if attempt.kueue_workload_uid is not None or attempt.scheduling_admission is not None
                    else LifecycleSource.CONTROLLER
                ),
                **common,
            )
        ]
        for pod_uid in attempt.pod_uids:
            correlations.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:pod:{pod_uid}",
                    source=LifecycleSource.KUBERNETES,
                    pod_uid=pod_uid,
                    **common,
                )
            )
        for node_uid in attempt.node_uids:
            correlations.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:node:{node_uid}",
                    source=LifecycleSource.KUBERNETES,
                    node_uid=node_uid,
                    **common,
                )
            )
        # The public result intentionally stores bounded identity sets.  Only a
        # single-Pod attempt preserves an exact Pod-to-device join; with more
        # Pods we retain the Pod facts and leave device attribution unavailable
        # rather than pairing sets by order.
        if len(attempt.pod_uids) == 1:
            pod_uid = attempt.pod_uids[0]
            for rank, gpu_uuid in enumerate(attempt.gpu_uuids):
                correlations.append(
                    LifecycleCorrelation(
                        correlation_key=f"{prefix}:pod:{pod_uid}:gpu:{gpu_uuid}:{rank}",
                        source=LifecycleSource.CONTROLLER,
                        pod_uid=pod_uid,
                        gpu_uuid=gpu_uuid,
                        gpu_rank=rank,
                        **common,
                    )
                )
        return correlations

    def _signals(
        self,
        result: ScientificRunResult,
        attempt: ResultAttempt,
        stage: StageSchedulingDecision,
    ) -> list[LifecycleSignal]:
        subject_id = _uuid(attempt.attempt_id, "attempt_id")
        prefix = f"scientific-result:{subject_id}"
        common: _SignalCommon = {
            "subject_id": subject_id,
            "observed_at": result.completed_at,
            "source_resolution_seconds": 0.0,
            "quality": MeasurementQuality.MEASURED,
            "edge": LifecycleEdge.INSTANT,
            "clock": LifecycleClock.LIFECYCLE,
            "attempt": attempt.attempt_number,
            "cluster": self.cluster,
            "namespace": result.scheduling_snapshot.workload_namespace,
            "queue_name": stage.resolved_local_queue,
            "kueue_workload_uid": attempt.kueue_workload_uid,
            "job_uid": attempt.k8s_job_uid,
        }
        signals: list[LifecycleSignal] = []
        if attempt.scheduling_admission is not None:
            signals.append(
                LifecycleSignal(
                    event_key=f"{prefix}:admitted",
                    occurred_at=attempt.scheduling_admission.admitted_at,
                    source=LifecycleSource.KUEUE,
                    phase=LifecyclePhase.ADMIT,
                    gpu_count=attempt.scheduling_admission.accelerator_count,
                    detail=self._admission_detail(result, stage, attempt),
                    **common,
                )
            )
        if attempt.attempt_number > 1:
            signals.append(
                LifecycleSignal(
                    event_key=f"{prefix}:retry:{attempt.attempt_number}",
                    occurred_at=attempt.started_at,
                    source=LifecycleSource.CONTROLLER,
                    phase=LifecyclePhase.RETRY,
                    gpu_count=0,
                    detail={"reason_code": "canonical_retry_attempt"},
                    **common,
                )
            )
        if attempt.status.value == "preempted":
            signals.append(
                LifecycleSignal(
                    event_key=f"{prefix}:preempted",
                    occurred_at=attempt.completed_at,
                    source=LifecycleSource.KUEUE,
                    phase=LifecyclePhase.PREEMPTION,
                    gpu_count=0,
                    detail={"outcome": attempt.status.value},
                    **common,
                )
            )
        return signals

    async def project(
        self,
        operation: OperationView,
        result: ScientificRunResult,
        *,
        reproducibility: ReproducibilityMetadata | None = None,
    ) -> tuple[LifecycleRollup, ...]:
        """Persist canonical terminal identities without latency accounting."""

        operation_id, batch_id, workload_id = self._validate_identity(operation, result)
        stages = _stage_map(result)
        metadata = reproducibility or ReproducibilityMetadata()
        rollups: list[LifecycleRollup] = []
        with self._tracer.start_as_current_span(
            "fs2.scientific.lifecycle.project",
            kind=SpanKind.CONSUMER,
        ) as span:
            span.set_attribute("fs2.operation.id", str(operation_id))
            span.set_attribute("fs2.batch.id", str(batch_id))
            span.set_attribute("fs2.workload.id", str(workload_id))
            span.set_attribute("fs2.tenant.id", operation.tenant_id)
            span.set_attribute("fs2.principal.id", operation.principal_id)
            span.set_attribute("fs2.api_key.id_hash", api_key_id_hash(operation.token_id))
            span.set_attribute("fs2.model.id", result.execution_identity.model_id)
            span.set_attribute("fs2.model.revision", result.execution_identity.model_revision)
            span.set_attribute("fs2.service_class", result.scheduling_snapshot.service_class.value)
            span.set_attribute("fs2.result.digest", result.digest)
            span.set_attribute("fs2.result.status", result.terminal_status.value)
            for attempt in result.attempts:
                subject_id = _uuid(attempt.attempt_id, "attempt_id")
                stage = stages[attempt.stage_id]
                with self._tracer.start_as_current_span(
                    "fs2.scientific.lifecycle.attempt",
                    kind=SpanKind.INTERNAL,
                ) as attempt_span:
                    attempt_span.set_attribute("fs2.attempt.id", str(subject_id))
                    attempt_span.set_attribute("fs2.attempt.number", attempt.attempt_number)
                    attempt_span.set_attribute("fs2.stage.id", attempt.stage_id)
                    attempt_span.set_attribute("fs2.queue.local", stage.resolved_local_queue)
                    attempt_span.set_attribute("fs2.queue.cluster", stage.resolved_cluster_queue)
                    if attempt.kueue_workload_uid is not None:
                        attempt_span.set_attribute("fs2.kueue.workload.uid", attempt.kueue_workload_uid)
                    if attempt.scheduling_admission is not None:
                        admission = attempt.scheduling_admission
                        attempt_span.set_attribute("fs2.gpu.count", admission.accelerator_count)
                        if admission.resolved_pool_id is not None:
                            attempt_span.set_attribute("fs2.kueue.pool", admission.resolved_pool_id)
                        if admission.admitted_resource_flavor is not None:
                            attempt_span.set_attribute(
                                "fs2.kueue.resource_flavor",
                                admission.admitted_resource_flavor,
                            )
                    await self.lifecycle.register_subject(
                        LifecycleSubject(
                            subject_id=subject_id,
                            workload_kind=WorkloadTelemetryKind.SCIENTIFIC_BATCH,
                            operation_id=operation_id,
                            request_id=operation_id,
                            batch_id=batch_id,
                            workload_id=workload_id,
                            attempt_id=subject_id,
                            tenant_id=operation.tenant_id,
                            principal_id=operation.principal_id,
                            api_key_id=operation.token_id,
                            api_key_fingerprint=api_key_id_hash(operation.token_id),
                            model_id=result.execution_identity.model_id,
                            model_revision=result.execution_identity.model_revision,
                            protocol=operation.protocol,
                            accepted_at=operation.accepted_at,
                            reproducibility=metadata,
                        )
                    )
                    await self.lifecycle.append_correlations(self._correlations(result, attempt, stage))
                    await self.lifecycle.append_signals(self._signals(result, attempt, stage))
                    rollup = await self.lifecycle.reconcile(
                        subject_id,
                        terminal=True,
                        outcome=attempt.status.value,
                    )
                    if rollup is None:  # pragma: no cover - repository contract violation
                        raise RuntimeError("scientific lifecycle subject disappeared during projection")
                    rollups.append(rollup)
        return tuple(rollups)
