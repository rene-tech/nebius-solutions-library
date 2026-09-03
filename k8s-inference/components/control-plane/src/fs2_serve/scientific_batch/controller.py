"""Small fenced state machine for Kueue-backed staged scientific jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    AttemptArtifactCommit,
    AttemptOutcome,
    BatchClaim,
    BatchEventDraft,
    BatchEventKind,
    BatchStatus,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    ResolvedArtifactMaterialization,
    ResourceClass,
    RuntimeArtifactLocalization,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ScientificStageState,
    StageStatus,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
    attempt_identity,
    workload_name,
)
from .protocols import (
    LegacyArtifactCommitReader,
    ScientificBatchArtifactLifecycle,
    ScientificBatchCluster,
    ScientificBatchRepository,
    ScientificBatchResultPublisher,
)


class ScientificBatchController:
    """Reconcile a claimed batch through fenced durable transitions.

    Workload names and attempt IDs are deterministic. Kubernetes apply/delete
    calls are idempotent, while every durable transition is a fenced CAS. Stage
    start first reserves attempt identities, then binds the applied workload
    UIDs, so a fast admission or controller crash cannot outrun durable state.
    """

    def __init__(
        self,
        *,
        repository: ScientificBatchRepository,
        cluster: ScientificBatchCluster,
        controller_id: str,
        namespace: str,
        result_publisher: ScientificBatchResultPublisher | None = None,
        artifact_lifecycle: ScientificBatchArtifactLifecycle | None = None,
        lease_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not controller_id:
            raise ValueError("controller_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.repository = repository
        self.cluster = cluster
        self.controller_id = controller_id
        self.namespace = namespace
        self.result_publisher = result_publisher
        self.artifact_lifecycle = artifact_lifecycle
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    async def admit(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        model_id: str,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        variant_id: str = "canonical-runtime",
        input_artifact_id: UUID | None = None,
        execution_plan: AdapterExecutionPlan | None = None,
        access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT,
        input_manifest: VerifiedInputManifest | None = None,
        runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = (),
    ) -> ScientificBatchState:
        """Bind a frozen admission snapshot to an existing durable Operation."""

        if scheduling.workload_namespace != self.namespace:
            raise ValueError("routed Kueue LocalQueue namespace differs from the controller namespace")
        return await self.repository.create(
            operation_id=operation_id,
            tenant_id=tenant_id,
            model_id=model_id,
            variant_id=variant_id,
            input_artifact_id=input_artifact_id or uuid5(NAMESPACE_URL, f"fs2-input:{operation_id}"),
            plan=plan,
            scheduling=scheduling,
            execution_plan=execution_plan,
            access_context=access_context,
            input_manifest=input_manifest,
            runtime_artifacts=runtime_artifacts,
        )

    async def reconcile_once(self) -> UUID | None:
        now = self.clock()
        claim = await self.repository.claim_next(
            controller_id=self.controller_id,
            lease_seconds=self.lease_seconds,
            now=now,
        )
        if claim is None:
            return None
        try:
            record = await self.repository.load(claim)
            await self._reconcile(claim, record, now=now)
            return claim.operation_id
        finally:
            await self.repository.release(claim)

    async def _reconcile(self, claim: BatchClaim, record: ScientificBatchState, *, now: datetime) -> None:
        if record.status.terminal:
            if not record.result_published:
                if self.result_publisher is None:
                    raise RuntimeError("terminal scientific batch has no result publisher")
                await self.result_publisher.publish_terminal(record)
                await self._write(
                    claim,
                    record,
                    replace(record, result_published=True),
                    (),
                    now=now,
                )
            return
        if record.cancel_requested:
            await self._cancel(claim, record, now=now)
            return

        active = next((stage for stage in record.stages if stage.status is StageStatus.ACTIVE), None)
        if active is not None:
            await self._reconcile_active_stage(claim, record, active, now=now)
            return

        if all(stage.status is StageStatus.SUCCEEDED for stage in record.stages):
            events = (self._event(record, BatchEventKind.BATCH_SUCCEEDED),)
            await self._write(
                claim,
                record,
                replace(
                    record,
                    status=BatchStatus.SUCCEEDED,
                    result_published=self.result_publisher is None,
                ),
                events,
                now=now,
            )
            return

        stage = await self._next_ready_stage(claim, record)
        if stage is not None:
            await self._start_stage(claim, record, stage, now=now)

    async def _next_ready_stage(self, claim: BatchClaim, record: ScientificBatchState) -> ScientificStagePlan | None:
        """Return one deterministic frontier stage after reopening predecessor commits."""

        for spec in record.plan.stages:
            stage = record.stage(spec.stage_id)
            if stage.status is not StageStatus.PENDING:
                continue
            ready = True
            for predecessor_id in spec.depends_on:
                predecessor = record.stage(predecessor_id)
                if predecessor.status is not StageStatus.SUCCEEDED:
                    ready = False
                    break
                commits = await self._artifact_commits(claim, record, stage_id=predecessor_id)
                if not self._commits_match(record, predecessor, commits):
                    ready = False
                    break
            if ready:
                return spec
        return None

    async def _start_stage(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        spec: ScientificStagePlan,
        *,
        now: datetime,
    ) -> None:
        stage = record.stage(spec.stage_id)
        attempts: list[ScientificAttemptState] = []
        events: list[BatchEventDraft] = []
        for shard_id in spec.workload_units:
            prior = stage.latest_attempt(shard_id)
            if prior is not None and prior.outcome is AttemptOutcome.SUCCEEDED:
                continue
            attempt_number = 1 if prior is None else prior.attempt_number + 1
            attempt_id = attempt_identity(record.operation_id, spec.stage_id, shard_id, attempt_number)
            kind = WorkloadKind.JOB if spec.mode is ExecutionMode.FANOUT else WorkloadKind.JOB_SET
            attempts.append(
                ScientificAttemptState(
                    attempt_id=attempt_id,
                    stage_id=spec.stage_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    workload=WorkloadRef(
                        namespace=record.scheduling.workload_namespace,
                        name=workload_name(record.operation_id, spec.stage_id, shard_id, attempt_number),
                        kind=kind,
                    ),
                    started_at=now,
                )
            )
            events.extend(
                (
                    self._event(
                        record,
                        BatchEventKind.LIFECYCLE,
                        stage_id=spec.stage_id,
                        shard_id=shard_id,
                        attempt_id=attempt_id,
                        phase=LifecyclePhase.QUEUED,
                    ),
                    self._event(
                        record,
                        BatchEventKind.LIFECYCLE,
                        stage_id=spec.stage_id,
                        shard_id=shard_id,
                        attempt_id=attempt_id,
                        phase=LifecyclePhase.SCHEDULING,
                    ),
                )
            )
            if attempt_number > 1:
                events.append(
                    self._event(
                        record,
                        BatchEventKind.RETRY_SCHEDULED,
                        stage_id=spec.stage_id,
                        shard_id=shard_id,
                        attempt_id=attempt_id,
                    )
                )

        replacement = replace(stage, status=StageStatus.ACTIVE, attempts=stage.attempts + tuple(attempts))
        next_record = self._replace_stage(record, replacement, status=BatchStatus.RUNNING)
        reserved = await self._write(claim, record, next_record, tuple(events), now=now)
        await self._apply_pending_attempts(claim, reserved, reserved.stage(spec.stage_id), now=now)

    async def _apply_pending_attempts(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        stage: ScientificStageState,
        *,
        now: datetime,
    ) -> None:
        spec = record.plan.stage(stage.stage_id)
        pending = [
            attempt
            for shard_id in spec.workload_units
            if (attempt := stage.latest_attempt(shard_id)) is not None
            and attempt.outcome is AttemptOutcome.ACTIVE
            and attempt.workload.uid is None
        ]
        if not pending:
            return
        attempts = list(stage.attempts)
        applied: list[WorkloadRef] = []
        try:
            for attempt in pending:
                invocation = (
                    record.execution_plan.invocation(spec.stage_id, attempt.shard_id)
                    if record.execution_plan is not None
                    else None
                )
                materializations = (
                    await self._resolve_materializations(claim, record, invocation) if invocation is not None else ()
                )
                resource = WorkloadResource(
                    operation_id=record.operation_id,
                    batch_id=record.batch_id,
                    workload_id=record.workload_id,
                    attempt_id=attempt.attempt_id,
                    stage_id=spec.stage_id,
                    shard_id=attempt.shard_id,
                    attempt_number=attempt.attempt_number,
                    tenant_id=record.tenant_id,
                    model_id=record.model_id,
                    variant_id=record.variant_id,
                    input_artifact_id=record.input_artifact_id,
                    service_class=record.scheduling.service_class,
                    scheduling_snapshot_digest=record.scheduling.digest,
                    namespace=attempt.workload.namespace,
                    name=attempt.workload.name,
                    kind=attempt.workload.kind,
                    scheduling=record.scheduling.stage(spec.stage_id),
                    gang_size=spec.gang_size,
                    invocation=invocation,
                    materializations=materializations,
                    access_context=record.access_context,
                    runtime_artifacts=tuple(
                        item
                        for item in record.runtime_artifacts
                        if invocation is not None and item.logical_artifact_id in invocation.runtime_artifacts
                    ),
                )
                ref = await self.cluster.apply(resource, controller_fence=claim.fencing_token)
                if (ref.namespace, ref.name, ref.kind) != (
                    resource.namespace,
                    resource.name,
                    resource.kind,
                ) or ref.uid is None:
                    raise RuntimeError("cluster apply returned an incomplete workload identity")
                applied.append(ref)
                if self.artifact_lifecycle is not None:
                    await self.artifact_lifecycle.open_attempt(resource, started_at=attempt.started_at or now)
                attempts[attempts.index(attempt)] = replace(attempt, workload=ref)
        except Exception:
            for ref in applied:
                await self.cluster.delete(ref, controller_fence=claim.fencing_token)
            raise
        applied_stage = replace(stage, attempts=tuple(attempts))
        await self._write(
            claim,
            record,
            self._replace_stage(record, applied_stage),
            (),
            now=now,
        )

    async def _reconcile_active_stage(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        stage: ScientificStageState,
        *,
        now: datetime,
    ) -> None:
        spec = record.plan.stage(stage.stage_id)
        latest = tuple(stage.latest_attempt(shard_id) for shard_id in spec.workload_units)
        if any(attempt is None for attempt in latest):
            raise RuntimeError("active stage has no attempt for every workload unit")
        current = tuple(attempt for attempt in latest if attempt is not None)
        if any(attempt.outcome is AttemptOutcome.ACTIVE and attempt.workload.uid is None for attempt in current):
            await self._apply_pending_attempts(claim, record, stage, now=now)
            return

        if stage.failure_code is not None:
            failed_attempt = next(
                (attempt for attempt in current if attempt.failure_kind is not None),
                None,
            )
            failure_kind = (
                failed_attempt.failure_kind
                if failed_attempt is not None and failed_attempt.failure_kind is not None
                else FailureKind.INFRASTRUCTURE
            )
            await self._fail_batch(
                claim,
                record,
                stage,
                failure_kind,
                stage.failure_code,
                [],
                now=now,
            )
            return

        fatal_attempt = next(
            (
                attempt
                for attempt in current
                if attempt.outcome in {AttemptOutcome.FAILED, AttemptOutcome.PREEMPTED}
                and (
                    attempt.failure_kind is None
                    or not attempt.failure_kind.retryable
                    or attempt.attempt_number >= spec.max_attempts
                )
            ),
            None,
        )
        if fatal_attempt is not None:
            failure_kind = fatal_attempt.failure_kind or FailureKind.INFRASTRUCTURE
            await self._fail_batch(
                claim,
                record,
                stage,
                failure_kind,
                fatal_attempt.failure_code or str(failure_kind),
                [],
                now=now,
            )
            return

        # A successful DELETE is only an accepted foreground-deletion request.
        # Persist that boundary, then poll this exact UID on a later reconcile;
        # quota/GPU accounting ends only after Kubernetes confirms absence.
        unreleased = [
            attempt
            for attempt in current
            if attempt.outcome is not AttemptOutcome.ACTIVE and not attempt.resource_released
        ]
        if unreleased:
            attempts = list(stage.attempts)
            cleanup_events: list[BatchEventDraft] = []
            for attempt in unreleased:
                cleaned_attempt, attempt_events = await self._advance_cleanup(
                    claim,
                    record,
                    attempt,
                    graceful=False,
                )
                cleanup_events.extend(attempt_events)
                attempts[attempts.index(attempt)] = cleaned_attempt
            cleaned = replace(stage, attempts=tuple(attempts))
            if cleaned != stage or cleanup_events:
                await self._write(
                    claim,
                    record,
                    self._replace_stage(record, cleaned),
                    tuple(cleanup_events),
                    now=now,
                )
            return

        active_attempts = [attempt for attempt in current if attempt.outcome is AttemptOutcome.ACTIVE]
        retryable_pending = any(
            attempt.outcome in {AttemptOutcome.FAILED, AttemptOutcome.PREEMPTED}
            and attempt.failure_kind is not None
            and attempt.failure_kind.retryable
            and attempt.attempt_number < spec.max_attempts
            for attempt in current
        )
        if retryable_pending and not active_attempts:
            pending = replace(stage, status=StageStatus.PENDING)
            await self._write(claim, record, self._replace_stage(record, pending), (), now=now)
            return

        all_succeeded = all(attempt.outcome is AttemptOutcome.SUCCEEDED for attempt in current)
        if all_succeeded:
            if self.artifact_lifecycle is not None:
                await self.artifact_lifecycle.ensure_stage_commit(record, stage_id=stage.stage_id)
            commits = await self._artifact_commits(claim, record, stage_id=stage.stage_id)
            if not commits:
                return
            if not self._commits_match(record, stage, commits):
                await self._fail_batch(
                    claim,
                    record,
                    stage,
                    FailureKind.SCIENTIFIC_VALIDATION,
                    "artifact_commit_fenced_or_invalid",
                    [],
                    now=now,
                )
                return
            succeeded = replace(stage, status=StageStatus.SUCCEEDED)
            success_events = (self._event(record, BatchEventKind.STAGE_SUCCEEDED, stage_id=stage.stage_id),)
            await self._write(claim, record, self._replace_stage(record, succeeded), success_events, now=now)
            return

        changed = False
        updated = list(stage.attempts)
        events: list[BatchEventDraft] = []

        for attempt in active_attempts:
            observation = await self.cluster.observe(attempt.workload)
            if observation.ref != attempt.workload or observation.attempt_id != attempt.attempt_id:
                events.append(
                    self._event(
                        record,
                        BatchEventKind.ATTEMPT_FENCED,
                        stage_id=stage.stage_id,
                        shard_id=attempt.shard_id,
                        attempt_id=observation.attempt_id,
                        code="stale_attempt_observation",
                    )
                )
                continue

            next_attempt, phase_events = self._ingest_observation(record, attempt, observation)
            events.extend(phase_events)
            if next_attempt != attempt:
                changed = True

            max_queue_seconds = record.scheduling.stage(stage.stage_id).max_queue_seconds
            queue_expired = (
                max_queue_seconds is not None
                and attempt.started_at is not None
                and next_attempt.last_phase.rank < LifecyclePhase.ADMITTED.rank
                and now >= attempt.started_at + timedelta(seconds=max_queue_seconds)
            )
            if queue_expired:
                next_attempt = replace(
                    next_attempt,
                    outcome=AttemptOutcome.FAILED,
                    completed_at=now,
                    failure_kind=FailureKind.APPLICATION,
                    failure_code="queue_deadline_exceeded",
                )
                changed = True

            elif observation.state.terminal:
                if observation.state is WorkloadState.SUCCEEDED:
                    next_attempt = replace(next_attempt, outcome=AttemptOutcome.SUCCEEDED, completed_at=now)
                else:
                    failure_kind = observation.failure_kind or FailureKind.INFRASTRUCTURE
                    outcome = (
                        AttemptOutcome.PREEMPTED
                        if observation.state is WorkloadState.PREEMPTED
                        else AttemptOutcome.FAILED
                    )
                    next_attempt = replace(
                        next_attempt,
                        outcome=outcome,
                        completed_at=now,
                        failure_kind=failure_kind,
                        failure_code=observation.failure_code or str(failure_kind),
                    )
                changed = True
            updated[updated.index(attempt)] = next_attempt

        next_stage = replace(stage, attempts=tuple(updated))
        if changed or events:
            await self._write(claim, record, self._replace_stage(record, next_stage), tuple(events), now=now)

    def _ingest_observation(
        self,
        record: ScientificBatchState,
        attempt: ScientificAttemptState,
        observation: WorkloadObservation,
    ) -> tuple[ScientificAttemptState, tuple[BatchEventDraft, ...]]:
        last = attempt.last_phase
        events: list[BatchEventDraft] = []
        for phase in observation.phases:
            if phase.rank <= last.rank:
                continue
            events.append(self._lifecycle(record, attempt, phase))
            last = phase
        if observation.state is WorkloadState.PREEMPTED and last.rank < LifecyclePhase.PREEMPTED.rank:
            events.append(self._lifecycle(record, attempt, LifecyclePhase.PREEMPTED))
            last = LifecyclePhase.PREEMPTED
        admission = observation.scheduling_admission or attempt.scheduling_admission
        if attempt.scheduling_admission is not None and observation.scheduling_admission not in {
            None,
            attempt.scheduling_admission,
        }:
            raise RuntimeError("Kueue scheduling admission changed for an immutable attempt")
        if LifecyclePhase.ADMITTED in observation.phases and admission is None:
            raise RuntimeError("Kueue admitted lifecycle phase has no resolved scheduling admission")
        if (
            observation.state is WorkloadState.SUCCEEDED
            or observation.state is WorkloadState.PREEMPTED
            or LifecyclePhase.ACTIVE_COMPUTE in observation.phases
        ) and admission is None:
            raise RuntimeError("Kueue-backed workload progressed without a resolved scheduling admission")
        if admission is not None:
            decision = record.scheduling.stage(attempt.stage_id)
            if (
                admission.accelerator_count != decision.accelerator_count
                or admission.accelerator_resource_name != decision.accelerator_resource_name
                or (
                    decision.resource_class is ResourceClass.GPU
                    and admission.resolved_pool_id not in decision.resolved_pool_preference
                )
                or admission.admitted_at < record.scheduling.captured_at
            ):
                raise RuntimeError("Kueue scheduling admission differs from the frozen stage request")
        kueue_uid = observation.kueue_workload_uid or attempt.kueue_workload_uid
        if attempt.kueue_workload_uid is not None and observation.kueue_workload_uid not in {
            None,
            attempt.kueue_workload_uid,
        }:
            raise RuntimeError("Kueue Workload UID changed for an immutable attempt")
        pod_uids = tuple(dict.fromkeys((*attempt.pod_uids, *observation.pod_uids)))
        return replace(
            attempt,
            last_phase=last,
            scheduling_admission=admission,
            kueue_workload_uid=kueue_uid,
            pod_uids=pod_uids,
        ), tuple(events)

    async def _fail_batch(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        stage: ScientificStageState,
        failure_kind: FailureKind,
        code: str,
        events: list[BatchEventDraft],
        *,
        now: datetime,
    ) -> None:
        attempts = list(stage.attempts)
        for index, attempt in enumerate(attempts):
            active = attempt.outcome is AttemptOutcome.ACTIVE
            terminal_attempt = replace(
                attempt,
                outcome=AttemptOutcome.CANCELLED if active else attempt.outcome,
                completed_at=now if active else attempt.completed_at,
                failure_kind=failure_kind if active else attempt.failure_kind,
                failure_code="peer_failed" if active else attempt.failure_code,
            )
            cleaned_attempt, cleanup_events = await self._advance_cleanup(
                claim,
                record,
                terminal_attempt,
                graceful=active,
            )
            events.extend(cleanup_events)
            attempts[index] = cleaned_attempt

        deletion_pending = any(not attempt.resource_released for attempt in attempts)
        failed = replace(
            stage,
            status=StageStatus.ACTIVE if deletion_pending else StageStatus.FAILED,
            attempts=tuple(attempts),
            failure_code=code,
        )
        if deletion_pending:
            next_record = replace(
                self._replace_stage(record, failed, status=BatchStatus.RUNNING),
                failure_code=code,
            )
            if next_record != record or events:
                await self._write(claim, record, next_record, tuple(events), now=now)
            return

        stages = tuple(
            failed
            if item.stage_id == stage.stage_id
            else replace(item, status=StageStatus.CANCELLED)
            if item.status is StageStatus.PENDING
            else item
            for item in record.stages
        )
        events.extend(
            (
                self._event(record, BatchEventKind.STAGE_FAILED, stage_id=stage.stage_id, code=code),
                self._event(record, BatchEventKind.BATCH_FAILED, code=code),
            )
        )
        next_record = replace(
            record,
            stages=stages,
            status=BatchStatus.FAILED,
            failure_code=code,
            result_published=self.result_publisher is None,
        )
        await self._write(claim, record, next_record, tuple(events), now=now)

    async def _cancel(self, claim: BatchClaim, record: ScientificBatchState, *, now: datetime) -> None:
        stages: list[ScientificStageState] = []
        events: list[BatchEventDraft] = []
        deletion_pending = False
        for stage in record.stages:
            attempts: list[ScientificAttemptState] = []
            for attempt in stage.attempts:
                active = attempt.outcome is AttemptOutcome.ACTIVE
                terminal_attempt = replace(
                    attempt,
                    outcome=AttemptOutcome.CANCELLED if active else attempt.outcome,
                    completed_at=now if active else attempt.completed_at,
                    failure_code="cancelled" if active else attempt.failure_code,
                )
                cleaned_attempt, cleanup_events = await self._advance_cleanup(
                    claim,
                    record,
                    terminal_attempt,
                    graceful=active,
                )
                events.extend(cleanup_events)
                deletion_pending = deletion_pending or not cleaned_attempt.resource_released
                attempts.append(cleaned_attempt)
            stage_deletion_pending = any(not attempt.resource_released for attempt in attempts)
            status = (
                stage.status
                if stage_deletion_pending or stage.status is StageStatus.SUCCEEDED
                else StageStatus.CANCELLED
            )
            stages.append(replace(stage, status=status, attempts=tuple(attempts)))
        if deletion_pending:
            next_record = replace(
                record,
                stages=tuple(stages),
                status=BatchStatus.RUNNING,
                failure_code="cancelled",
            )
            if next_record != record or events:
                await self._write(claim, record, next_record, tuple(events), now=now)
            return

        events.append(self._event(record, BatchEventKind.BATCH_CANCELLED, code="cancelled"))
        next_record = replace(
            record,
            stages=tuple(stages),
            status=BatchStatus.CANCELLED,
            failure_code="cancelled",
            result_published=self.result_publisher is None,
        )
        await self._write(claim, record, next_record, tuple(events), now=now)

    async def _advance_cleanup(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        attempt: ScientificAttemptState,
        *,
        graceful: bool,
    ) -> tuple[ScientificAttemptState, tuple[BatchEventDraft, ...]]:
        """Advance exactly one persisted step of UID-fenced workload cleanup."""

        if attempt.outcome is AttemptOutcome.ACTIVE:
            raise RuntimeError("cannot clean up an active scientific attempt")
        if attempt.resource_released:
            return attempt, ()
        events: list[BatchEventDraft] = []
        if attempt.workload.uid is None:
            if attempt.last_phase is not LifecyclePhase.TEARDOWN:
                events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
            return replace(attempt, last_phase=LifecyclePhase.TEARDOWN, resource_released=True), tuple(events)
        if not attempt.deletion_requested:
            if self.artifact_lifecycle is not None:
                await self.artifact_lifecycle.close_attempt(record, attempt)
            await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
            last_phase = attempt.last_phase
            if graceful and last_phase.rank < LifecyclePhase.GRACE_DRAIN.rank:
                events.append(self._lifecycle(record, attempt, LifecyclePhase.GRACE_DRAIN))
                last_phase = LifecyclePhase.GRACE_DRAIN
            return replace(attempt, deletion_requested=True, last_phase=last_phase), tuple(events)
        if not await self.cluster.absent(attempt.workload):
            return attempt, ()
        if attempt.last_phase is not LifecyclePhase.TEARDOWN:
            events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
        return replace(attempt, last_phase=LifecyclePhase.TEARDOWN, resource_released=True), tuple(events)

    async def _resolve_materializations(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        invocation: object,
    ) -> tuple[ResolvedArtifactMaterialization, ...]:
        from .models import StageInvocation

        if not isinstance(invocation, StageInvocation):
            raise RuntimeError("scientific workload has no canonical stage invocation")
        resolved: list[ResolvedArtifactMaterialization] = []
        for item in invocation.materializations:
            if record.input_manifest is not None:
                try:
                    source = record.input_manifest.artifact(item.artifact_id)
                except ValueError:
                    source = None
            else:
                source = None
            if source is not None:
                resolved.append(
                    ResolvedArtifactMaterialization.resolve(
                        item,
                        artifact_id=source.artifact_id,
                        digest=source.digest,
                        size_bytes=source.size_bytes,
                        media_type=source.media_type,
                        compression=source.compression,
                    )
                )
            else:
                if record.execution_plan is None:
                    raise RuntimeError("logical artifact handoff has no adapter execution plan")
                producer = record.execution_plan.producer(item.artifact_id)
                if producer is None:
                    raise RuntimeError("logical artifact has no producer")
                commits = await self._artifact_commits(claim, record, stage_id=producer.stage_id)
                matches = [commit for commit in commits if commit.logical_artifact_id == item.artifact_id]
                if len(matches) != 1:
                    raise RuntimeError("logical predecessor artifact has no unique fenced commit")
                commit = matches[0]
                resolved.append(
                    ResolvedArtifactMaterialization.resolve(
                        item,
                        artifact_id=commit.handoff_artifact_id,
                        digest=commit.handoff_digest,
                        size_bytes=commit.handoff_size_bytes,
                        media_type=commit.handoff_media_type,
                        compression=commit.handoff_compression,
                    )
                )
        return tuple(resolved)

    async def _artifact_commits(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        *,
        stage_id: str,
    ) -> tuple[AttemptArtifactCommit, ...]:
        if self.artifact_lifecycle is not None:
            return await self.artifact_lifecycle.artifact_commits(record, stage_id=stage_id)
        # Core-only tests deliberately keep the fake commit ledger on the fake
        # repository. Production always uses the artifact-service lifecycle.
        legacy = cast(LegacyArtifactCommitReader, self.repository)
        return await legacy.artifact_commits(claim, stage_id=stage_id)

    @staticmethod
    def _commits_match(
        record: ScientificBatchState,
        stage: ScientificStageState,
        commits: tuple[object, ...],
    ) -> bool:
        from .models import AttemptArtifactCommit

        if not commits or any(
            not isinstance(commit, AttemptArtifactCommit) or not commit.semantic_valid for commit in commits
        ):
            return False
        successful = {attempt.attempt_id for attempt in stage.attempts if attempt.outcome is AttemptOutcome.SUCCEEDED}
        committed = {commit.attempt_ids[0] for commit in commits if isinstance(commit, AttemptArtifactCommit)}
        if successful != committed or len(commits) != len(successful):
            return False
        expected_logical = None
        if record.execution_plan is not None:
            expected_logical = {
                record.execution_plan.invocation(stage.stage_id, attempt.shard_id).produces
                for attempt in stage.attempts
                if attempt.outcome is AttemptOutcome.SUCCEEDED
            }
        actual_logical = {commit.logical_artifact_id for commit in commits if isinstance(commit, AttemptArtifactCommit)}
        expected_bindings = (
            {}
            if record.execution_plan is None
            else {
                attempt.attempt_id: record.execution_plan.invocation(stage.stage_id, attempt.shard_id)
                for attempt in stage.attempts
                if attempt.outcome is AttemptOutcome.SUCCEEDED
            }
        )
        return all(
            isinstance(commit, AttemptArtifactCommit)
            and commit.operation_id == record.operation_id
            and commit.stage_id == stage.stage_id
            and (
                record.execution_plan is None
                or (
                    commit.attempt_ids[0] in expected_bindings
                    and commit.collector_id == expected_bindings[commit.attempt_ids[0]].collector_id
                    and commit.validator_id == expected_bindings[commit.attempt_ids[0]].validator_id
                )
            )
            for commit in commits
        ) and (expected_logical is None or actual_logical == expected_logical)

    async def _write(
        self,
        claim: BatchClaim,
        current: ScientificBatchState,
        replacement: ScientificBatchState,
        events: tuple[BatchEventDraft, ...],
        *,
        now: datetime,
    ) -> ScientificBatchState:
        return await self.repository.replace(
            claim,
            expected_revision=current.revision,
            record=replace(replacement, revision=current.revision + 1),
            events=events,
            now=now,
        )

    @staticmethod
    def _replace_stage(
        record: ScientificBatchState,
        stage: ScientificStageState,
        *,
        status: BatchStatus | None = None,
    ) -> ScientificBatchState:
        stages = tuple(stage if item.stage_id == stage.stage_id else item for item in record.stages)
        return replace(record, stages=stages, status=status or record.status)

    @staticmethod
    def _event(
        record: ScientificBatchState,
        kind: BatchEventKind,
        *,
        stage_id: str | None = None,
        shard_id: str | None = None,
        attempt_id: UUID | None = None,
        phase: LifecyclePhase | None = None,
        code: str | None = None,
    ) -> BatchEventDraft:
        return BatchEventDraft.build(
            operation_id=record.operation_id,
            batch_id=record.batch_id,
            workload_id=record.workload_id,
            kind=kind,
            stage_id=stage_id,
            shard_id=shard_id,
            attempt_id=attempt_id,
            phase=phase,
            code=code,
        )

    def _lifecycle(
        self,
        record: ScientificBatchState,
        attempt: ScientificAttemptState,
        phase: LifecyclePhase,
    ) -> BatchEventDraft:
        return self._event(
            record,
            BatchEventKind.LIFECYCLE,
            stage_id=attempt.stage_id,
            shard_id=attempt.shard_id,
            attempt_id=attempt.attempt_id,
            phase=phase,
        )
