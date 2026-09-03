"""Replayable scientific-run projection into the canonical lifecycle ledger."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ..lifecycle import (
    LifecycleClock,
    LifecycleCorrelation,
    LifecycleEdge,
    LifecycleRepository,
    LifecycleSignal,
    LifecycleSource,
    LifecycleSubject,
    MeasurementQuality,
    ReproducibilityMetadata,
    WorkloadTelemetryKind,
    api_key_id_hash,
)
from ..lifecycle import (
    LifecyclePhase as LedgerPhase,
)
from ..models import OperationView
from .models import (
    AttemptOutcome,
    BatchEvent,
    BatchEventKind,
    CheckpointMode,
    LifecyclePhase,
    PodLifecycleObservation,
    ScientificAttemptState,
    ScientificBatchState,
    WorkloadObservation,
)


class ScientificBatchEventSource(Protocol):
    async def list_events(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[BatchEvent]: ...


class ScientificOperationSource(Protocol):
    async def get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> OperationView: ...


_PHASE_MAP: Mapping[LifecyclePhase, LedgerPhase] = {
    LifecyclePhase.IMAGE_LOADING: LedgerPhase.IMAGE_PULL,
    LifecyclePhase.ARTIFACT_LOADING: LedgerPhase.ARTIFACT_LOAD,
    LifecyclePhase.RESTORING: LedgerPhase.RESTORE,
    LifecyclePhase.SEMANTIC_WARMUP: LedgerPhase.WARMUP,
    LifecyclePhase.ACTIVE_COMPUTE: LedgerPhase.ACTIVE_COMPUTE,
    LifecyclePhase.ALLOCATED_IDLE: LedgerPhase.RESIDENT_IDLE,
    LifecyclePhase.TEARDOWN: LedgerPhase.TEARDOWN,
}
_REQUIRED_ACCOUNTING_PHASES = (
    LedgerPhase.IMAGE_PULL,
    LedgerPhase.ARTIFACT_LOAD,
    LedgerPhase.RESTORE,
    LedgerPhase.WARMUP,
    LedgerPhase.ACTIVE_COMPUTE,
    LedgerPhase.RESIDENT_IDLE,
    LedgerPhase.COOLDOWN_GRACE,
    LedgerPhase.CHECKPOINT_DRAIN,
    LedgerPhase.TEARDOWN,
)


def _phase_for_event(
    state: ScientificBatchState,
    attempt: ScientificAttemptState,
    phase: LifecyclePhase,
) -> LedgerPhase:
    if phase is LifecyclePhase.GRACE_DRAIN:
        checkpoint = state.scheduling.stage(attempt.stage_id).checkpoint_mode
        return LedgerPhase.CHECKPOINT_DRAIN if checkpoint is not CheckpointMode.NONE else LedgerPhase.COOLDOWN_GRACE
    return _PHASE_MAP[phase]


def _event_time(events: Sequence[BatchEvent], phase: LifecyclePhase) -> datetime | None:
    return next(
        (
            event.occurred_at.astimezone(UTC)
            for event in events
            if event.draft.kind is BatchEventKind.LIFECYCLE and event.draft.phase is phase
        ),
        None,
    )


class ScientificLifecycleBridge:
    """Project controller and Kubernetes facts into one append-only ledger.

    The controller's existing batch-event table is the replay source. Every
    projection key is derived from immutable attempt/event/Pod identities, so
    restart after either side of a database write is safe. There is no second
    lifecycle table or display-only state.
    """

    def __init__(
        self,
        *,
        lifecycle: LifecycleRepository,
        batches: ScientificBatchEventSource,
        operations: ScientificOperationSource,
        cluster: str | None = None,
        source_resolution_seconds: float = 5.0,
    ) -> None:
        if cluster is not None and (not cluster or len(cluster) > 128):
            raise ValueError("scientific lifecycle cluster identity is invalid")
        if not 0 <= source_resolution_seconds <= 300:
            raise ValueError("scientific lifecycle source resolution is outside the bound")
        self.lifecycle = lifecycle
        self.batches = batches
        self.operations = operations
        self.cluster = cluster
        self.source_resolution_seconds = source_resolution_seconds

    async def _events(self, state: ScientificBatchState) -> list[BatchEvent]:
        result: list[BatchEvent] = []
        after = 0
        while True:
            page = await self.batches.list_events(
                state.operation_id,
                tenant_id=state.tenant_id,
                after_sequence=after,
                limit=1000,
            )
            result.extend(page)
            if len(page) < 1000:
                return result
            after = page[-1].sequence

    @staticmethod
    def _attempts(state: ScientificBatchState) -> tuple[ScientificAttemptState, ...]:
        return tuple(attempt for stage in state.stages for attempt in stage.attempts)

    async def _subject(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        operation: OperationView,
    ) -> None:
        if (
            operation.id != state.operation_id
            or operation.tenant_id != state.tenant_id
            or operation.model_id != state.model_id
            or operation.protocol != "scientific-batch-v1"
        ):
            raise RuntimeError("scientific lifecycle operation identity differs from the frozen batch")
        await self.lifecycle.register_subject(
            LifecycleSubject(
                subject_id=attempt.attempt_id,
                workload_kind=WorkloadTelemetryKind.SCIENTIFIC_BATCH,
                operation_id=state.operation_id,
                request_id=state.operation_id,
                batch_id=state.batch_id,
                workload_id=state.workload_id,
                attempt_id=attempt.attempt_id,
                tenant_id=state.tenant_id,
                principal_id=operation.principal_id,
                api_key_id=operation.token_id,
                api_key_fingerprint=api_key_id_hash(operation.token_id),
                model_id=state.model_id,
                model_revision=operation.model_revision,
                protocol=operation.protocol,
                accepted_at=operation.accepted_at,
                reproducibility=ReproducibilityMetadata(),
            )
        )

    def _correlations(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
    ) -> list[LifecycleCorrelation]:
        prefix = f"scientific:{attempt.attempt_id}"
        observed_at = (attempt.started_at or state.scheduling.captured_at).astimezone(UTC)
        scheduling = state.scheduling.stage(attempt.stage_id)
        result = [
            LifecycleCorrelation(
                correlation_key=f"{prefix}:queue",
                subject_id=attempt.attempt_id,
                observed_at=observed_at,
                source=LifecycleSource.CONTROLLER,
                attempt=attempt.attempt_number,
                cluster=self.cluster,
                namespace=attempt.workload.namespace,
                queue_name=scheduling.resolved_local_queue,
            )
        ]
        if attempt.workload.uid is not None:
            result.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:job:{attempt.workload.uid}",
                    subject_id=attempt.attempt_id,
                    observed_at=observed_at,
                    source=LifecycleSource.KUBERNETES,
                    attempt=attempt.attempt_number,
                    cluster=self.cluster,
                    namespace=attempt.workload.namespace,
                    job_name=attempt.workload.name,
                    job_uid=attempt.workload.uid,
                )
            )
        if attempt.kueue_workload_uid is not None:
            result.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:kueue:{attempt.kueue_workload_uid}",
                    subject_id=attempt.attempt_id,
                    observed_at=observed_at,
                    source=LifecycleSource.KUEUE,
                    attempt=attempt.attempt_number,
                    cluster=self.cluster,
                    namespace=attempt.workload.namespace,
                    queue_name=scheduling.resolved_local_queue,
                    kueue_workload_uid=attempt.kueue_workload_uid,
                    job_name=attempt.workload.name,
                    job_uid=attempt.workload.uid,
                )
            )
        return result

    @staticmethod
    def _signal(
        *,
        event_key: str,
        subject_id: UUID,
        occurred_at: datetime,
        phase: LedgerPhase,
        edge: LifecycleEdge,
        clock: LifecycleClock,
        attempt: ScientificAttemptState,
        source: LifecycleSource,
        quality: MeasurementQuality,
        interval_key: str | None = None,
        observed_at: datetime | None = None,
        source_resolution_seconds: float = 0,
        gpu_count: int = 0,
        cluster: str | None = None,
        queue_name: str | None = None,
        kueue_workload_uid: str | None = None,
        pod: PodLifecycleObservation | None = None,
        gpu_uuid: str | None = None,
        gpu_rank: int | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> LifecycleSignal:
        occurred_at = occurred_at.astimezone(UTC)
        observed = (observed_at or occurred_at).astimezone(UTC)
        return LifecycleSignal(
            event_key=event_key,
            subject_id=subject_id,
            occurred_at=occurred_at,
            observed_at=max(occurred_at, observed),
            source=source,
            source_resolution_seconds=source_resolution_seconds,
            quality=quality,
            phase=phase,
            edge=edge,
            clock=clock,
            interval_key=interval_key,
            attempt=attempt.attempt_number,
            gpu_count=gpu_count,
            cluster=cluster,
            namespace=attempt.workload.namespace,
            queue_name=queue_name,
            kueue_workload_uid=kueue_workload_uid,
            job_name=attempt.workload.name,
            job_uid=attempt.workload.uid,
            pod_name=None if pod is None else pod.pod_name,
            pod_uid=None if pod is None else pod.pod_uid,
            # Pod, node and device facts are independently immutable
            # correlations. Omitting node enrichment here keeps replay safe
            # when a later poll resolves the Node UID for an existing Pod.
            node_name=None,
            node_uid=None,
            gpu_uuid=gpu_uuid,
            gpu_rank=gpu_rank,
            detail=dict(detail or {}),
        )

    def _event_signals(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        events: Sequence[BatchEvent],
    ) -> list[LifecycleSignal]:
        attempt_events = [event for event in events if event.draft.attempt_id == attempt.attempt_id]
        attempt_events.sort(key=lambda event: event.sequence)
        prefix = f"scientific:{attempt.attempt_id}"
        scheduling = state.scheduling.stage(attempt.stage_id)
        signals: list[LifecycleSignal] = []
        queued_at = _event_time(attempt_events, LifecyclePhase.QUEUED)
        admitted_event_at = _event_time(attempt_events, LifecyclePhase.ADMITTED)
        teardown_at = _event_time(attempt_events, LifecyclePhase.TEARDOWN)
        admission = attempt.scheduling_admission
        quota_at = None if admission is None else admission.quota_reserved_at or admission.admitted_at
        admitted_at = None if admission is None else admission.admitted_at
        if queued_at is not None:
            signals.append(
                self._signal(
                    event_key=f"{prefix}:enqueue",
                    subject_id=attempt.attempt_id,
                    occurred_at=queued_at,
                    phase=LedgerPhase.ENQUEUE,
                    edge=LifecycleEdge.INSTANT,
                    clock=LifecycleClock.LIFECYCLE,
                    attempt=attempt,
                    source=LifecycleSource.CONTROLLER,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    cluster=self.cluster,
                    queue_name=scheduling.resolved_local_queue,
                )
            )
            queue_end = quota_at or admitted_at or admitted_event_at or teardown_at
            interval = f"{prefix}:queue"
            signals.append(
                self._signal(
                    event_key=f"{interval}:start",
                    subject_id=attempt.attempt_id,
                    occurred_at=queued_at,
                    phase=LedgerPhase.ADMISSION_WAIT,
                    edge=LifecycleEdge.START,
                    clock=LifecycleClock.LIFECYCLE,
                    interval_key=interval,
                    attempt=attempt,
                    source=LifecycleSource.CONTROLLER,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    cluster=self.cluster,
                    queue_name=scheduling.resolved_local_queue,
                )
            )
            if queue_end is not None:
                signals.append(
                    self._signal(
                        event_key=f"{interval}:end",
                        subject_id=attempt.attempt_id,
                        occurred_at=max(queued_at, queue_end),
                        phase=LedgerPhase.ADMISSION_WAIT,
                        edge=LifecycleEdge.END,
                        clock=LifecycleClock.LIFECYCLE,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.CONTROLLER,
                        quality=MeasurementQuality.APPLICATION_OBSERVED,
                        cluster=self.cluster,
                        queue_name=scheduling.resolved_local_queue,
                    )
                )
        if admitted_at is not None or admitted_event_at is not None:
            exact_admitted_at = admitted_at or admitted_event_at
            assert exact_admitted_at is not None
            signals.append(
                self._signal(
                    event_key=f"{prefix}:admitted",
                    subject_id=attempt.attempt_id,
                    occurred_at=exact_admitted_at,
                    observed_at=admitted_event_at,
                    phase=LedgerPhase.ADMIT,
                    edge=LifecycleEdge.INSTANT,
                    clock=LifecycleClock.LIFECYCLE,
                    attempt=attempt,
                    source=LifecycleSource.KUEUE,
                    quality=MeasurementQuality.MEASURED,
                    cluster=self.cluster,
                    queue_name=scheduling.resolved_local_queue,
                    kueue_workload_uid=attempt.kueue_workload_uid,
                )
            )
        if (
            quota_at is not None
            and admission is not None
            and admission.accelerator_count > 0
            and attempt.kueue_workload_uid is not None
        ):
            interval = f"{prefix}:quota:{attempt.kueue_workload_uid}"
            signals.append(
                self._signal(
                    event_key=f"{interval}:start",
                    subject_id=attempt.attempt_id,
                    occurred_at=quota_at,
                    phase=LedgerPhase.ADMIT,
                    edge=LifecycleEdge.START,
                    clock=LifecycleClock.QUOTA_RESERVED,
                    interval_key=interval,
                    attempt=attempt,
                    source=LifecycleSource.KUEUE,
                    quality=MeasurementQuality.MEASURED,
                    source_resolution_seconds=self.source_resolution_seconds,
                    gpu_count=admission.accelerator_count,
                    cluster=self.cluster,
                    queue_name=scheduling.resolved_local_queue,
                    kueue_workload_uid=attempt.kueue_workload_uid,
                )
            )
            if teardown_at is not None:
                signals.append(
                    self._signal(
                        event_key=f"{interval}:end",
                        subject_id=attempt.attempt_id,
                        occurred_at=max(quota_at, teardown_at),
                        phase=LedgerPhase.ADMIT,
                        edge=LifecycleEdge.END,
                        clock=LifecycleClock.QUOTA_RESERVED,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.KUEUE,
                        quality=MeasurementQuality.MEASURED,
                        source_resolution_seconds=self.source_resolution_seconds,
                        gpu_count=admission.accelerator_count,
                        cluster=self.cluster,
                        queue_name=scheduling.resolved_local_queue,
                        kueue_workload_uid=attempt.kueue_workload_uid,
                    )
                )

        transition_events = [
            event
            for event in attempt_events
            if event.draft.kind is BatchEventKind.LIFECYCLE and event.draft.phase is not None
        ]
        # Kubernetes container timestamps own model-runtime phase accounting.
        # Controller events remain the durable source for the cleanup phases
        # which occur after the Pod observer has stopped reporting execution.
        accounting_events = [
            event
            for event in transition_events
            if event.draft.phase in {LifecyclePhase.GRACE_DRAIN, LifecyclePhase.TEARDOWN}
        ]
        for event in accounting_events:
            assert event.draft.phase is not None
            phase = _phase_for_event(state, attempt, event.draft.phase)
            end = next(
                (candidate.occurred_at for candidate in transition_events if candidate.sequence > event.sequence),
                teardown_at,
            )
            interval = f"{prefix}:controller-phase:{event.draft.event_id}"
            signals.append(
                self._signal(
                    event_key=f"{interval}:start",
                    subject_id=attempt.attempt_id,
                    occurred_at=event.occurred_at,
                    phase=phase,
                    edge=LifecycleEdge.START,
                    clock=LifecycleClock.PHASE,
                    interval_key=interval,
                    attempt=attempt,
                    source=LifecycleSource.CONTROLLER,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    source_resolution_seconds=self.source_resolution_seconds,
                    cluster=self.cluster,
                )
            )
            if end is not None:
                signals.append(
                    self._signal(
                        event_key=f"{interval}:end",
                        subject_id=attempt.attempt_id,
                        occurred_at=max(event.occurred_at, end),
                        phase=phase,
                        edge=LifecycleEdge.END,
                        clock=LifecycleClock.PHASE,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.CONTROLLER,
                        quality=MeasurementQuality.APPLICATION_OBSERVED,
                        source_resolution_seconds=self.source_resolution_seconds,
                        cluster=self.cluster,
                    )
                )
        preempted_at = _event_time(attempt_events, LifecyclePhase.PREEMPTED)
        if preempted_at is not None:
            signals.append(
                self._signal(
                    event_key=f"{prefix}:preempted",
                    subject_id=attempt.attempt_id,
                    occurred_at=preempted_at,
                    phase=LedgerPhase.PREEMPTION,
                    edge=LifecycleEdge.INSTANT,
                    clock=LifecycleClock.LIFECYCLE,
                    attempt=attempt,
                    source=LifecycleSource.KUEUE,
                    quality=MeasurementQuality.MEASURED,
                    cluster=self.cluster,
                    detail={"reason_code": attempt.failure_code or "preempted"},
                )
            )
        if teardown_at is not None:
            signals.append(
                self._signal(
                    event_key=f"{prefix}:terminal",
                    subject_id=attempt.attempt_id,
                    occurred_at=teardown_at,
                    phase=LedgerPhase.RELEASE,
                    edge=LifecycleEdge.INSTANT,
                    clock=LifecycleClock.LIFECYCLE,
                    attempt=attempt,
                    source=LifecycleSource.CONTROLLER,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    cluster=self.cluster,
                    detail={"outcome": attempt.outcome.value},
                )
            )
        return signals

    def _pod_correlations(
        self,
        attempt: ScientificAttemptState,
        pod: PodLifecycleObservation,
    ) -> list[LifecycleCorrelation]:
        prefix = f"scientific:{attempt.attempt_id}:pod:{pod.pod_uid}"
        observed_at = pod.scheduled_at or min(
            (phase.started_at for phase in pod.phases),
            default=pod.observed_at,
        )
        result = [
            LifecycleCorrelation(
                correlation_key=prefix,
                subject_id=attempt.attempt_id,
                observed_at=observed_at,
                source=LifecycleSource.KUBERNETES,
                attempt=attempt.attempt_number,
                cluster=self.cluster,
                namespace=attempt.workload.namespace,
                job_name=attempt.workload.name,
                job_uid=attempt.workload.uid,
                pod_name=pod.pod_name,
                pod_uid=pod.pod_uid,
            )
        ]
        if pod.node_uid is not None:
            result.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:node:{pod.node_uid}",
                    subject_id=attempt.attempt_id,
                    observed_at=observed_at,
                    source=LifecycleSource.KUBERNETES,
                    attempt=attempt.attempt_number,
                    cluster=self.cluster,
                    namespace=attempt.workload.namespace,
                    job_name=attempt.workload.name,
                    job_uid=attempt.workload.uid,
                    pod_name=pod.pod_name,
                    pod_uid=pod.pod_uid,
                    node_name=pod.node_name,
                    node_uid=pod.node_uid,
                )
            )
        for rank, gpu_uuid in enumerate(pod.gpu_uuids):
            result.append(
                LifecycleCorrelation(
                    correlation_key=f"{prefix}:device:{gpu_uuid}:{rank}",
                    subject_id=attempt.attempt_id,
                    observed_at=observed_at,
                    source=LifecycleSource.KUBELET,
                    attempt=attempt.attempt_number,
                    cluster=self.cluster,
                    namespace=attempt.workload.namespace,
                    job_name=attempt.workload.name,
                    job_uid=attempt.workload.uid,
                    pod_name=pod.pod_name,
                    pod_uid=pod.pod_uid,
                    gpu_uuid=gpu_uuid,
                    gpu_rank=rank,
                )
            )
        return result

    def _pod_signals(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        pod: PodLifecycleObservation,
    ) -> list[LifecycleSignal]:
        prefix = f"scientific:{attempt.attempt_id}:pod:{pod.pod_uid}"
        signals: list[LifecycleSignal] = []
        if pod.scheduled_at is not None and pod.gpu_count > 0:
            interval = f"{prefix}:scheduler"
            signals.append(
                self._signal(
                    event_key=f"{interval}:start",
                    subject_id=attempt.attempt_id,
                    occurred_at=pod.scheduled_at,
                    observed_at=pod.scheduled_at,
                    phase=LedgerPhase.GPU_ALLOCATION,
                    edge=LifecycleEdge.START,
                    clock=LifecycleClock.SCHEDULER_OCCUPIED,
                    interval_key=interval,
                    attempt=attempt,
                    source=LifecycleSource.KUBERNETES,
                    quality=MeasurementQuality.MEASURED,
                    source_resolution_seconds=self.source_resolution_seconds,
                    gpu_count=pod.gpu_count,
                    cluster=self.cluster,
                    pod=pod,
                )
            )
        device_start = pod.scheduled_at or min(
            (phase.started_at for phase in pod.phases),
            default=None,
        )
        if device_start is not None:
            for rank, gpu_uuid in enumerate(pod.gpu_uuids):
                interval = f"{prefix}:device:{gpu_uuid}:{rank}"
                signals.append(
                    self._signal(
                        event_key=f"{interval}:start",
                        subject_id=attempt.attempt_id,
                        occurred_at=device_start,
                        observed_at=device_start,
                        phase=LedgerPhase.GPU_ALLOCATION,
                        edge=LifecycleEdge.START,
                        clock=LifecycleClock.DEVICE_ALLOCATED,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.KUBELET,
                        quality=MeasurementQuality.MEASURED,
                        source_resolution_seconds=self.source_resolution_seconds,
                        gpu_count=1,
                        cluster=self.cluster,
                        pod=pod,
                        gpu_uuid=gpu_uuid,
                        gpu_rank=rank,
                    )
                )
        for value in pod.phases:
            phase = _phase_for_event(state, attempt, value.phase)
            interval = f"{prefix}:phase:{phase.value}:{value.started_at.timestamp():.6f}"
            signals.append(
                self._signal(
                    event_key=f"{interval}:start",
                    subject_id=attempt.attempt_id,
                    occurred_at=value.started_at,
                    observed_at=value.started_at,
                    phase=phase,
                    edge=LifecycleEdge.START,
                    clock=LifecycleClock.PHASE,
                    interval_key=interval,
                    attempt=attempt,
                    source=LifecycleSource.KUBERNETES,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    source_resolution_seconds=self.source_resolution_seconds,
                    cluster=self.cluster,
                    pod=pod,
                )
            )
            if value.ended_at is not None:
                signals.append(
                    self._signal(
                        event_key=f"{interval}:end",
                        subject_id=attempt.attempt_id,
                        occurred_at=value.ended_at,
                        observed_at=value.ended_at,
                        phase=phase,
                        edge=LifecycleEdge.END,
                        clock=LifecycleClock.PHASE,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.KUBERNETES,
                        quality=MeasurementQuality.APPLICATION_OBSERVED,
                        source_resolution_seconds=self.source_resolution_seconds,
                        cluster=self.cluster,
                        pod=pod,
                    )
                )
        return signals

    async def observe(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        observation: WorkloadObservation,
    ) -> None:
        operation = await self.operations.get_operation(state.operation_id, tenant_id=state.tenant_id)
        await self._subject(state, attempt, operation)
        correlations: list[LifecycleCorrelation] = []
        signals: list[LifecycleSignal] = []
        for pod in observation.pod_lifecycle:
            correlations.extend(self._pod_correlations(attempt, pod))
            signals.extend(self._pod_signals(state, attempt, pod))
        await self.lifecycle.append_correlations(correlations)
        await self.lifecycle.append_signals(signals)

    async def _append_unobserved_phases(
        self,
        state: ScientificBatchState,
        attempt: ScientificAttemptState,
        released_at: datetime,
    ) -> None:
        detail = await self.lifecycle.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
        present = (
            set()
            if detail is None
            else {
                signal.phase
                for signal in detail.signals
                if signal.clock is LifecycleClock.PHASE and signal.edge is LifecycleEdge.START
            }
        )
        signals: list[LifecycleSignal] = []
        # Inapplicable or unobservable phases are explicit zero-length edges.
        # They make the complete contract queryable without inventing elapsed
        # GPU time; phases already captured from Pod/container evidence retain
        # their measured quality.
        for phase in _REQUIRED_ACCOUNTING_PHASES:
            if phase in present:
                continue
            interval = f"scientific:{attempt.attempt_id}:unobserved:{phase.value}"
            for edge in (LifecycleEdge.START, LifecycleEdge.END):
                signals.append(
                    self._signal(
                        event_key=f"{interval}:{edge.value}",
                        subject_id=attempt.attempt_id,
                        occurred_at=released_at,
                        phase=phase,
                        edge=edge,
                        clock=LifecycleClock.PHASE,
                        interval_key=interval,
                        attempt=attempt,
                        source=LifecycleSource.DERIVED,
                        quality=MeasurementQuality.UNAVAILABLE,
                        cluster=self.cluster,
                    )
                )
        await self.lifecycle.append_signals(signals)

    async def _close_open_intervals(
        self,
        attempt: ScientificAttemptState,
        released_at: datetime,
        *,
        tenant_id: str,
    ) -> None:
        detail = await self.lifecycle.get_workload(attempt.attempt_id, tenant_id=tenant_id)
        if detail is None:
            return
        starts = {
            signal.interval_key: signal
            for signal in detail.signals
            if signal.edge is LifecycleEdge.START and signal.interval_key is not None
        }
        ended = {
            signal.interval_key
            for signal in detail.signals
            if signal.edge is LifecycleEdge.END and signal.interval_key is not None
        }
        closing: list[LifecycleSignal] = []
        for interval_key, start in starts.items():
            if interval_key in ended or start.clock not in {
                LifecycleClock.SCHEDULER_OCCUPIED,
                LifecycleClock.DEVICE_ALLOCATED,
                LifecycleClock.PHASE,
            }:
                continue
            closing.append(
                start.model_copy(
                    update={
                        "sequence": None,
                        "event_key": f"{interval_key}:terminal-end",
                        "occurred_at": max(start.occurred_at, released_at),
                        "observed_at": max(start.occurred_at, released_at),
                        "source": LifecycleSource.CONTROLLER,
                        "quality": MeasurementQuality.ESTIMATED,
                        "edge": LifecycleEdge.END,
                    }
                )
            )
        await self.lifecycle.append_signals(closing)

    async def sync(self, state: ScientificBatchState) -> None:
        events = await self._events(state)
        operation = await self.operations.get_operation(state.operation_id, tenant_id=state.tenant_id)
        for attempt in self._attempts(state):
            await self._subject(state, attempt, operation)
            await self.lifecycle.append_correlations(self._correlations(state, attempt))
            await self.lifecycle.append_signals(self._event_signals(state, attempt, events))
            teardown_at = _event_time(
                [event for event in events if event.draft.attempt_id == attempt.attempt_id],
                LifecyclePhase.TEARDOWN,
            )
            terminal = (
                attempt.outcome is not AttemptOutcome.ACTIVE
                and attempt.resource_released
                and teardown_at is not None
            )
            if terminal:
                assert teardown_at is not None
                await self._append_unobserved_phases(state, attempt, teardown_at)
                await self._close_open_intervals(attempt, teardown_at, tenant_id=state.tenant_id)
            await self.lifecycle.reconcile(
                attempt.attempt_id,
                terminal=terminal,
                outcome=attempt.outcome.value if terminal else None,
            )
