"""Small fenced state machine for Kueue-backed staged scientific jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    AttemptOutcome,
    BatchClaim,
    BatchEventDraft,
    BatchEventKind,
    BatchStatus,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    ResolvedArtifactMaterialization,
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
    WorkloadResource,
    WorkloadState,
    attempt_identity,
    workload_name,
)
from .protocols import ScientificBatchCluster, ScientificBatchRepository, ScientificBatchResultPublisher


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
        result_publisher: ScientificBatchResultPublisher | None = None,
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
                commits = await self.repository.artifact_commits(claim, stage_id=predecessor_id)
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
                invocation = (
                    record.execution_plan.invocation(spec.stage_id, shard_id)
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
                    attempt_id=attempt_id,
                    stage_id=spec.stage_id,
                    shard_id=shard_id,
                    attempt_number=attempt_number,
                    tenant_id=record.tenant_id,
                    model_id=record.model_id,
                    variant_id=record.variant_id,
                    input_artifact_id=record.input_artifact_id,
                    service_class=record.scheduling.service_class,
                    scheduling_snapshot_digest=record.scheduling.digest,
                    namespace=self.namespace,
                    name=workload_name(record.operation_id, spec.stage_id, shard_id, attempt_number),
                    kind=kind,
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

        # Terminal observations are persisted before deleting their workload.
        # Deletion is idempotent, so a crash between the external mutation and
        # this CAS only repeats cleanup; it can never leave an ACTIVE record
        # pointing at a Job that was already removed.
        unreleased = [
            attempt
            for attempt in current
            if attempt.outcome is not AttemptOutcome.ACTIVE and not attempt.resource_released
        ]
        if unreleased:
            attempts = list(stage.attempts)
            cleanup_events: list[BatchEventDraft] = []
            for attempt in unreleased:
                await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
                if attempt.last_phase is not LifecyclePhase.TEARDOWN:
                    cleanup_events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
                attempts[attempts.index(attempt)] = replace(
                    attempt,
                    last_phase=LifecyclePhase.TEARDOWN,
                    resource_released=True,
                )
            cleaned = replace(stage, attempts=tuple(attempts))
            cleaned_current = tuple(cleaned.latest_attempt(shard_id) for shard_id in spec.workload_units)
            if any(attempt is None for attempt in cleaned_current):
                raise RuntimeError("cleaned stage has no attempt for every workload unit")
            cleaned_attempts = tuple(attempt for attempt in cleaned_current if attempt is not None)
            fatal_attempt = next(
                (
                    attempt
                    for attempt in cleaned_attempts
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
                    cleaned,
                    failure_kind,
                    fatal_attempt.failure_code or str(failure_kind),
                    cleanup_events,
                    now=now,
                )
                return
            if not any(attempt.outcome is AttemptOutcome.ACTIVE for attempt in cleaned_attempts) and any(
                attempt.outcome in {AttemptOutcome.FAILED, AttemptOutcome.PREEMPTED} for attempt in cleaned_attempts
            ):
                pending = replace(cleaned, status=StageStatus.PENDING)
                await self._write(
                    claim,
                    record,
                    self._replace_stage(record, pending),
                    tuple(cleanup_events),
                    now=now,
                )
                return
            if all(attempt.outcome is AttemptOutcome.SUCCEEDED for attempt in cleaned_attempts):
                commits = await self.repository.artifact_commits(claim, stage_id=stage.stage_id)
                if commits and not self._commits_match(record, cleaned, commits):
                    await self._fail_batch(
                        claim,
                        record,
                        cleaned,
                        FailureKind.SCIENTIFIC_VALIDATION,
                        "artifact_commit_fenced_or_invalid",
                        cleanup_events,
                        now=now,
                    )
                    return
                if commits:
                    succeeded = replace(cleaned, status=StageStatus.SUCCEEDED)
                    cleanup_events.append(self._event(record, BatchEventKind.STAGE_SUCCEEDED, stage_id=stage.stage_id))
                    await self._write(
                        claim,
                        record,
                        self._replace_stage(record, succeeded),
                        tuple(cleanup_events),
                        now=now,
                    )
                    return
            await self._write(
                claim,
                record,
                self._replace_stage(record, cleaned),
                tuple(cleanup_events),
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
            commits = await self.repository.artifact_commits(claim, stage_id=stage.stage_id)
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

            if observation.state.terminal:
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
            if not attempt.resource_released:
                await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
                if (
                    attempt.outcome is AttemptOutcome.ACTIVE
                    and attempt.last_phase.rank < LifecyclePhase.GRACE_DRAIN.rank
                ):
                    events.append(self._lifecycle(record, attempt, LifecyclePhase.GRACE_DRAIN))
                if attempt.last_phase is not LifecyclePhase.TEARDOWN:
                    events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
            active = attempt.outcome is AttemptOutcome.ACTIVE
            attempt = replace(
                attempt,
                outcome=AttemptOutcome.CANCELLED if active else attempt.outcome,
                last_phase=LifecyclePhase.TEARDOWN if not attempt.resource_released else attempt.last_phase,
                resource_released=True,
                failure_kind=failure_kind if active else attempt.failure_kind,
                failure_code="peer_failed" if active else attempt.failure_code,
            )
            attempts[index] = attempt
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
        for stage in record.stages:
            attempts: list[ScientificAttemptState] = []
            for attempt in stage.attempts:
                if not attempt.resource_released:
                    await self.cluster.delete(attempt.workload, controller_fence=claim.fencing_token)
                    if (
                        attempt.outcome is AttemptOutcome.ACTIVE
                        and attempt.last_phase.rank < LifecyclePhase.GRACE_DRAIN.rank
                    ):
                        events.append(self._lifecycle(record, attempt, LifecyclePhase.GRACE_DRAIN))
                    if attempt.last_phase is not LifecyclePhase.TEARDOWN:
                        events.append(self._lifecycle(record, attempt, LifecyclePhase.TEARDOWN))
                active = attempt.outcome is AttemptOutcome.ACTIVE
                attempt = replace(
                    attempt,
                    outcome=AttemptOutcome.CANCELLED if active else attempt.outcome,
                    last_phase=LifecyclePhase.TEARDOWN if not attempt.resource_released else attempt.last_phase,
                    resource_released=True,
                    failure_code="cancelled" if active else attempt.failure_code,
                )
                attempts.append(attempt)
            status = stage.status if stage.status is StageStatus.SUCCEEDED else StageStatus.CANCELLED
            stages.append(replace(stage, status=status, attempts=tuple(attempts)))
        events.append(self._event(record, BatchEventKind.BATCH_CANCELLED, code="cancelled"))
        next_record = replace(
            record,
            stages=tuple(stages),
            status=BatchStatus.CANCELLED,
            failure_code="cancelled",
            result_published=self.result_publisher is None,
        )
        await self._write(claim, record, next_record, tuple(events), now=now)

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
                commits = await self.repository.artifact_commits(claim, stage_id=producer.stage_id)
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

    @staticmethod
    def _commits_match(
        record: ScientificBatchState,
        stage: ScientificStageState,
        commits: tuple[object, ...],
    ) -> bool:
        from .models import ArtifactCommit

        if not commits or any(
            not isinstance(commit, ArtifactCommit) or not commit.semantic_valid for commit in commits
        ):
            return False
        successful = {attempt.attempt_id for attempt in stage.attempts if attempt.outcome is AttemptOutcome.SUCCEEDED}
        committed = {commit.attempt_ids[0] for commit in commits if isinstance(commit, ArtifactCommit)}
        if successful != committed or len(commits) != len(successful):
            return False
        expected_logical = None
        if record.execution_plan is not None:
            expected_logical = {
                record.execution_plan.invocation(stage.stage_id, attempt.shard_id).produces
                for attempt in stage.attempts
                if attempt.outcome is AttemptOutcome.SUCCEEDED
            }
        actual_logical = {commit.logical_artifact_id for commit in commits if isinstance(commit, ArtifactCommit)}
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
            isinstance(commit, ArtifactCommit)
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
