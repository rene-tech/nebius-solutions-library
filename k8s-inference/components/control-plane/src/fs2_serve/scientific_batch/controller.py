"""Small fenced state machine for Kueue-backed staged scientific jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from .models import (
    AdapterExecutionPlan,
    AttemptOutcome,
    BatchClaim,
    BatchEventDraft,
    BatchEventKind,
    BatchStatus,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ScientificStageState,
    StageStatus,
    WorkloadKind,
    WorkloadObservation,
    WorkloadResource,
    WorkloadState,
    attempt_identity,
    workload_name,
)
from .protocols import ScientificBatchCluster, ScientificBatchRepository


class ScientificBatchController:
    """Reconcile at most one durable batch transition per claim.

    Workload names and attempt IDs are deterministic. Kubernetes apply/delete
    calls are idempotent, while every durable transition is a fenced CAS.
    """

    def __init__(
        self,
        *,
        repository: ScientificBatchRepository,
        cluster: ScientificBatchCluster,
        controller_id: str,
        namespace: str,
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
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    async def admit(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        execution_plan: AdapterExecutionPlan | None = None,
    ) -> ScientificBatchState:
        """Bind a frozen admission snapshot to an existing durable Operation."""

        return await self.repository.create(
            operation_id=operation_id,
            tenant_id=tenant_id,
            plan=plan,
            scheduling=scheduling,
            execution_plan=execution_plan,
        )

    async def admit_adapter_run(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        model_id: str,
        variant_id: str,
        profile: Mapping[str, object],
        request: object,
        scheduling: SchedulingSnapshot,
    ) -> ScientificBatchState:
        """Compile an allow-listed model request before freezing admission.

        The adapter can only select bounded catalog expansions and exact argv;
        scheduling remains an independently resolved, controller-owned input.
        """

        from .adapters import compile_adapter_run

        execution = compile_adapter_run(
            model_id,
            profile,
            request,
            operation_id=str(operation_id),
            variant_id=variant_id,
        )
        return await self.admit(
            operation_id=operation_id,
            tenant_id=tenant_id,
            plan=execution.controller_plan,
            scheduling=scheduling,
            execution_plan=execution,
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
            await self._write(claim, record, replace(record, status=BatchStatus.SUCCEEDED), events, now=now)
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
                commit = await self.repository.artifact_commit(claim, stage_id=predecessor_id)
                if not self._commit_matches(record, predecessor, commit):
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
        applied = []
        events: list[BatchEventDraft] = []
        try:
            for shard_id in spec.workload_units:
                prior = stage.latest_attempt(shard_id)
                if prior is not None and prior.outcome is AttemptOutcome.SUCCEEDED:
                    continue
                attempt_number = 1 if prior is None else prior.attempt_number + 1
                attempt_id = attempt_identity(record.operation_id, spec.stage_id, shard_id, attempt_number)
                kind = WorkloadKind.JOB if spec.mode is ExecutionMode.FANOUT else WorkloadKind.JOB_SET
                resource = WorkloadResource(
                    operation_id=record.operation_id,
                    batch_id=record.batch_id,
                    workload_id=record.workload_id,
                    attempt_id=attempt_id,
                    stage_id=spec.stage_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    namespace=self.namespace,
                    name=workload_name(record.operation_id, spec.stage_id, shard_id, attempt_number),
                    kind=kind,
                    scheduling=record.scheduling.stage(spec.stage_id),
                    gang_size=spec.gang_size,
                    invocation=(
                        record.execution_plan.invocation(spec.stage_id, shard_id)
                        if record.execution_plan is not None
                        else None
                    ),
                    model_id=record.model_id,
                    variant_id=record.variant_id,
                )
                ref = await self.cluster.apply(resource, controller_fence=claim.fencing_token)
                if (ref.namespace, ref.name, ref.kind) != (resource.namespace, resource.name, resource.kind):
                    raise RuntimeError("cluster apply returned a different workload identity")
                applied.append(ref)
                attempts.append(
                    ScientificAttemptState(
                        attempt_id=attempt_id,
                        stage_id=spec.stage_id,
                        shard_id=shard_id,
                        attempt_number=attempt_number,
                        workload=ref,
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
        except Exception:
            for ref in applied:
                await self.cluster.delete(ref, controller_fence=claim.fencing_token)
            raise

        replacement = replace(stage, status=StageStatus.ACTIVE, attempts=stage.attempts + tuple(attempts))
        next_record = self._replace_stage(record, replacement, status=BatchStatus.RUNNING)
        await self._write(claim, record, next_record, tuple(events), now=now)

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
        active_attempts = [attempt for attempt in current if attempt.outcome is AttemptOutcome.ACTIVE]

        changed = False
        updated = list(stage.attempts)
        events: list[BatchEventDraft] = []
        fatal: tuple[FailureKind, str] | None = None
        retry_needed = False

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

            if observation.state.terminal:
                await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
                if next_attempt.last_phase is not LifecyclePhase.TEARDOWN:
                    next_attempt = replace(next_attempt, last_phase=LifecyclePhase.TEARDOWN)
                    events.append(self._lifecycle(record, next_attempt, LifecyclePhase.TEARDOWN))
                if observation.state is WorkloadState.SUCCEEDED:
                    next_attempt = replace(next_attempt, outcome=AttemptOutcome.SUCCEEDED)
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
                        failure_kind=failure_kind,
                        failure_code=observation.failure_code or str(failure_kind),
                    )
                    if failure_kind.retryable and attempt.attempt_number < spec.max_attempts:
                        retry_needed = True
                    else:
                        fatal = (failure_kind, observation.failure_code or str(failure_kind))
                changed = True
            updated[updated.index(attempt)] = next_attempt

        next_stage = replace(stage, attempts=tuple(updated))
        if fatal is not None:
            await self._fail_batch(claim, record, next_stage, fatal[0], fatal[1], events, now=now)
            return

        latest_after = tuple(next_stage.latest_attempt(shard_id) for shard_id in spec.workload_units)
        all_succeeded = all(
            attempt is not None and attempt.outcome is AttemptOutcome.SUCCEEDED for attempt in latest_after
        )
        if all_succeeded:
            commit = await self.repository.artifact_commit(claim, stage_id=stage.stage_id)
            if commit is None:
                await self._fail_batch(
                    claim,
                    record,
                    next_stage,
                    FailureKind.SCIENTIFIC_VALIDATION,
                    "artifact_commit_missing",
                    events,
                    now=now,
                )
                return
            if not self._commit_matches(record, next_stage, commit):
                await self._fail_batch(
                    claim,
                    record,
                    next_stage,
                    FailureKind.SCIENTIFIC_VALIDATION,
                    "artifact_commit_fenced_or_invalid",
                    events,
                    now=now,
                )
                return
            succeeded = replace(next_stage, status=StageStatus.SUCCEEDED)
            events.append(self._event(record, BatchEventKind.STAGE_SUCCEEDED, stage_id=stage.stage_id))
            await self._write(claim, record, self._replace_stage(record, succeeded), tuple(events), now=now)
            return

        retryable_pending = retry_needed or any(
            attempt is not None
            and attempt.outcome in {AttemptOutcome.FAILED, AttemptOutcome.PREEMPTED}
            and attempt.failure_kind is not None
            and attempt.failure_kind.retryable
            and attempt.attempt_number < spec.max_attempts
            for attempt in latest_after
        )
        if retryable_pending and not any(
            attempt is not None and attempt.outcome is AttemptOutcome.ACTIVE for attempt in latest_after
        ):
            pending = replace(next_stage, status=StageStatus.PENDING)
            await self._write(claim, record, self._replace_stage(record, pending), tuple(events), now=now)
            return

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
        return replace(attempt, last_phase=last), tuple(events)

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
            if attempt.outcome is not AttemptOutcome.ACTIVE:
                continue
            await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
            if attempt.last_phase.rank < LifecyclePhase.GRACE_DRAIN.rank:
                events.append(self._lifecycle(record, attempt, LifecyclePhase.GRACE_DRAIN))
            events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
            attempts[index] = replace(
                attempt,
                outcome=AttemptOutcome.CANCELLED,
                last_phase=LifecyclePhase.TEARDOWN,
                failure_kind=failure_kind,
                failure_code="peer_failed",
            )
        failed = replace(stage, status=StageStatus.FAILED, attempts=tuple(attempts), failure_code=code)
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
        next_record = replace(record, stages=stages, status=BatchStatus.FAILED, failure_code=code)
        await self._write(claim, record, next_record, tuple(events), now=now)

    async def _cancel(self, claim: BatchClaim, record: ScientificBatchState, *, now: datetime) -> None:
        stages: list[ScientificStageState] = []
        events: list[BatchEventDraft] = []
        for stage in record.stages:
            attempts: list[ScientificAttemptState] = []
            for attempt in stage.attempts:
                if attempt.outcome is AttemptOutcome.ACTIVE:
                    await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
                    if attempt.last_phase.rank < LifecyclePhase.GRACE_DRAIN.rank:
                        events.append(self._lifecycle(record, attempt, LifecyclePhase.GRACE_DRAIN))
                    events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
                    attempt = replace(
                        attempt,
                        outcome=AttemptOutcome.CANCELLED,
                        last_phase=LifecyclePhase.TEARDOWN,
                        failure_code="cancelled",
                    )
                attempts.append(attempt)
            status = stage.status if stage.status is StageStatus.SUCCEEDED else StageStatus.CANCELLED
            stages.append(replace(stage, status=status, attempts=tuple(attempts)))
        events.append(self._event(record, BatchEventKind.BATCH_CANCELLED, code="cancelled"))
        next_record = replace(record, stages=tuple(stages), status=BatchStatus.CANCELLED, failure_code="cancelled")
        await self._write(claim, record, next_record, tuple(events), now=now)

    @staticmethod
    def _commit_matches(record: ScientificBatchState, stage: ScientificStageState, commit: object | None) -> bool:
        from .models import ArtifactCommit

        if not isinstance(commit, ArtifactCommit) or not commit.semantic_valid:
            return False
        successful = tuple(
            sorted(
                (attempt.attempt_id for attempt in stage.attempts if attempt.outcome is AttemptOutcome.SUCCEEDED),
                key=str,
            )
        )
        return (
            commit.operation_id == record.operation_id
            and commit.stage_id == stage.stage_id
            and tuple(sorted(commit.attempt_ids, key=str)) == successful
        )

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
            model_id=record.model_id,
            variant_id=record.variant_id,
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
