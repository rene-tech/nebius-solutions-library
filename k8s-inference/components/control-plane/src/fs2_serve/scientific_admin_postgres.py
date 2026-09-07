"""PostgreSQL-backed scientific admin projections.

The adapter reads only bounded, payload-free controller and operation fields.
It intentionally leaves GPU accounting unavailable until the exact lifecycle
ledger publishes allocation identities and closed GPU intervals.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import asyncpg

from .lifecycle import _signal_from_row
from .registry import Registry
from .scientific_admin import (
    ScientificAdminQueryError,
    ScientificAdminReadService,
    ScientificAdminSourceUnavailableError,
    ScientificArtifactAttemptEvidence,
    ScientificArtifactSnapshot,
    ScientificModelAdminAdapter,
    ScientificModelPolicyInvalidError,
    ScientificModelPolicySnapshot,
    ScientificModelPolicyStaleRevisionError,
    ScientificRunCancelOutcome,
    ScientificRunDetailSnapshot,
    ScientificRunListSnapshot,
    ScientificRunQuery,
)
from .scientific_admin_accounting import project_gpu_accounting
from .scientific_admin_catalog import (
    ScientificCatalogFileAdapter,
    ScientificProfileDiscoveryAdapter,
    scientific_receipts_file,
)
from .scientific_admin_models import (
    ScientificArtifact,
    ScientificArtifactDownload,
    ScientificAttempt,
    ScientificAttribution,
    ScientificBackendIdentity,
    ScientificCancellation,
    ScientificError,
    ScientificEvidenceState,
    ScientificFastStartObservation,
    ScientificGpuAccounting,
    ScientificLifecyclePhase,
    ScientificMeasurement,
    ScientificModelPolicy,
    ScientificModelPolicyList,
    ScientificModelPolicySetting,
    ScientificModelPolicyUpdate,
    ScientificModelReadiness,
    ScientificObservabilityLink,
    ScientificQueueState,
    ScientificRetry,
    ScientificRunDetail,
    ScientificRunList,
    ScientificRunModel,
    ScientificRunSummary,
    ScientificSemanticValidation,
    ScientificServiceClass,
    ScientificServiceClassDecision,
    ScientificStage,
    ScientificStageCounts,
    ScientificStageStartupPolicy,
)
from .scientific_admin_models import (
    ScientificDispatchCounts as ScientificDispatchCountsModel,
)
from .scientific_admin_models import (
    ScientificDispatchState as ScientificDispatchStateModel,
)
from .scientific_admin_phase_times import project_phase_times
from .scientific_artifacts import ArtifactNotFoundError, ScientificArtifactControllerPort
from .scientific_batch.codec import state_from_value
from .scientific_batch.models import (
    AttemptOutcome,
    BatchEvent,
    BatchStatus,
    LifecyclePhase,
    ResourceClass,
    ScientificAttemptState,
    ScientificBatchState,
    ScientificStageState,
    StageStatus,
    WorkloadKind,
)
from .scientific_batch.policy import (
    PostgresScientificModelPolicyRepository,
    ScientificModelPolicyRecord,
    ScientificModelPolicyStaleError,
    ScientificModelPolicyView,
)
from .scientific_batch.postgres_repository import PostgresScientificBatchRepository, ScientificBatchNotFoundError
from .scientific_batch.service import ScientificBatchService
from .scientific_run_result import ArtifactRef

_TERMINAL_BATCH_STATUS = {BatchStatus.SUCCEEDED, BatchStatus.FAILED, BatchStatus.CANCELLED}


def _bounded(value: object, maximum: int, fallback: str) -> str:
    text = str(value) if value is not None else fallback
    return (text or fallback)[:maximum]


def _unavailable(unit: str, reason: str) -> ScientificMeasurement:
    return ScientificMeasurement(
        value=None,
        unit=cast(Any, unit),
        evidence=ScientificEvidenceState.UNAVAILABLE,
        source="lifecycle-ledger",
        reason=reason,
    )


def _encode_cursor(accepted_at: datetime, operation_id: UUID) -> str:
    raw = f"{accepted_at.astimezone(UTC).isoformat()}|{operation_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    if len(value) > 512:
        raise ScientificAdminQueryError("scientific run cursor is outside the bound")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()
        accepted, operation = decoded.split("|", 1)
        accepted_at = datetime.fromisoformat(accepted)
        operation_id = UUID(operation)
    except (UnicodeDecodeError, ValueError) as error:
        raise ScientificAdminQueryError("scientific run cursor is invalid") from error
    if accepted_at.tzinfo is None:
        raise ScientificAdminQueryError("scientific run cursor is invalid")
    return accepted_at, operation_id


def _stage_status(stage: ScientificStageState) -> str:
    if stage.status is StageStatus.PENDING:
        return "pending"
    if stage.status is StageStatus.SUCCEEDED:
        return "succeeded"
    if stage.status is StageStatus.FAILED:
        return "failed"
    if stage.status is StageStatus.CANCELLED:
        return "cancelled"
    if not stage.attempts:
        return "queued"
    active = [item for item in _latest_shards(stage) if item.outcome is AttemptOutcome.ACTIVE]
    if any(item.last_phase.rank >= LifecyclePhase.IMAGE_LOADING.rank for item in active):
        return "running"
    if any(item.last_phase in {LifecyclePhase.ADMITTED, LifecyclePhase.NODE_PENDING} for item in active):
        return "admitted"
    return "queued"


def _latest_shards(stage: ScientificStageState) -> list[ScientificAttemptState]:
    """Current state per independent shard, excluding superseded retries."""
    latest: dict[str | None, ScientificAttemptState] = {}
    for attempt in stage.attempts:
        previous = latest.get(attempt.shard_id)
        if previous is None or attempt.attempt_number > previous.attempt_number:
            latest[attempt.shard_id] = attempt
    return list(latest.values())


def _shard_counts(stage: ScientificStageState) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for attempt in _latest_shards(stage):
        if attempt.outcome is not AttemptOutcome.ACTIVE:
            label = attempt.outcome.value
        elif attempt.last_phase in {LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING}:
            label = "pending"
        elif attempt.last_phase in {LifecyclePhase.ADMITTED, LifecyclePhase.NODE_PENDING}:
            label = "admitted"
        else:
            label = "running"
        counts[label] += 1
    return dict(counts)


def _run_status(state: ScientificBatchState) -> str:
    if state.cancel_requested and not state.status.terminal:
        return "cancelling"
    if state.status is BatchStatus.SUCCEEDED:
        return "succeeded"
    if state.status is BatchStatus.FAILED:
        return "failed"
    if state.status is BatchStatus.CANCELLED:
        return "cancelled"
    if any(_stage_status(stage) == "running" for stage in state.stages):
        return "running"
    if any(_stage_status(stage) == "admitted" for stage in state.stages):
        return "admitted"
    return "queued"


def _active_stage(state: ScientificBatchState) -> ScientificStageState:
    return next(
        (stage for stage in state.stages if stage.status is StageStatus.ACTIVE),
        next((stage for stage in state.stages if not stage.status.terminal), state.stages[-1]),
    )


_DISPATCH_HOLD_REASONS = {
    "paused": "Dispatch is held: new work for this model is paused by operator policy.",
    "concurrency": "Dispatch is held: the model's active-run cap set by operator policy is reached.",
}


def _admission_state(state: ScientificBatchState, dispatch_hold: str | None = None) -> tuple[str, str]:
    if state.status in _TERMINAL_BATCH_STATUS:
        return "finished", "The durable scientific batch is terminal."
    if state.status is BatchStatus.QUEUED and not state.cancel_requested and dispatch_hold is not None:
        return "pending", _DISPATCH_HOLD_REASONS.get(dispatch_hold, "Dispatch is held by the scientific model policy.")
    stage = _active_stage(state)
    counts = _shard_counts(stage)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    if counts.get("admitted", 0) + counts.get("running", 0):
        return "admitted", f"Current {stage.stage_id} shards: {summary}. Pending shards await their own admission."
    if counts.get("preempted", 0):
        return "evicted", "The latest attempt was preempted and awaits controller reconciliation."
    return "pending", f"Current {stage.stage_id} shards: {summary or 'not yet created'}. Awaiting admission."


def _stage_counts(state: ScientificBatchState) -> ScientificStageCounts:
    counts = {name: 0 for name in ("pending", "queued", "admitted", "running", "succeeded", "failed", "cancelled")}
    for stage in state.stages:
        counts[_stage_status(stage)] += 1
    return ScientificStageCounts(
        pending=counts["pending"],
        queued=counts["queued"],
        admitted=counts["admitted"],
        running=counts["running"],
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        cancelled=counts["cancelled"],
        skipped=0,
    )


def _error(state: ScientificBatchState) -> ScientificError | None:
    code = state.failure_code or next((stage.failure_code for stage in state.stages if stage.failure_code), None)
    if code is None:
        return None
    retryable = any(
        attempt.failure_kind is not None and attempt.failure_kind.retryable
        for stage in state.stages
        for attempt in stage.attempts
    )
    bounded = _bounded(code, 64, "scientific_batch_failed")
    return ScientificError(code=bounded, message=f"Scientific run failed with code {bounded}.", retryable=retryable)


def _gpu_accounting() -> ScientificGpuAccounting:
    return ScientificGpuAccounting(
        gpu_count=None,
        capacity_type="unknown",
        allocated=_unavailable("gpu-seconds", "No exact GPU allocation boundary is available."),
        active=_unavailable("gpu-seconds", "No exact GPU active-compute interval is available."),
        idle_total=_unavailable("gpu-seconds", "No exact GPU allocation boundary is available."),
        idle_by_cause=[],
        grace_drain=_unavailable("gpu-seconds", "No exact GPU grace or drain interval is available."),
        reconciliation_delta=_unavailable("gpu-seconds", "The GPU lifecycle cannot be reconciled exactly."),
    )


def _summary(
    record: Mapping[str, Any],
    state: ScientificBatchState,
    model: ScientificModelReadiness,
) -> ScientificRunSummary:
    execution_mode = model.execution_mode
    if execution_mode is None:
        raise ScientificAdminSourceUnavailableError(
            f"scientific model {state.model_id} has no published execution profile"
        )
    stage = _active_stage(state)
    scheduling = state.scheduling.stage(stage.stage_id)
    dispatch_hold = record.get("dispatch_hold")
    admission_state, admission_reason = _admission_state(state, None if dispatch_hold is None else str(dispatch_hold))
    effective = state.scheduling.service_class.value
    terminal = state.status in _TERMINAL_BATCH_STATUS
    cancel_requested_at = record.get("cancel_requested_at")
    cancel_actor = record.get("cancel_actor")
    backend = model.backend.model_copy(update={"model_revision": _bounded(record["model_revision"], 256, "unknown")})
    return ScientificRunSummary(
        id=str(state.operation_id),
        batch_id=str(state.batch_id),
        display_name=_bounded(f"{model.display_name} · {record['operation']}", 200, state.model_id),
        operation=_bounded(record["operation"], 64, "scientific-run"),
        status=cast(Any, _run_status(state)),
        submitted_at=record["accepted_at"],
        completed_at=record.get("completed_at"),
        attribution=ScientificAttribution(
            tenant_id=_bounded(record["tenant_id"], 120, "unknown"),
            user_id=_bounded(record.get("created_by"), 200, "unknown"),
            principal_id=_bounded(record["principal_id"], 200, "unknown"),
            api_key_prefix=_bounded(record["token_prefix"], 64, "unknown"),
        ),
        model=ScientificRunModel(
            model_id=state.model_id,
            display_name=model.display_name,
            execution_mode=execution_mode,
            backend=ScientificBackendIdentity.model_validate(backend),
        ),
        access=model.access,
        service_class=ScientificServiceClassDecision(
            requested=cast(Any, effective),
            effective=cast(Any, effective),
            reason="The validated request was frozen into the immutable scheduling snapshot.",
            policy_revision=state.scheduling.policy_revision,
        ),
        queue=ScientificQueueState(
            tenant_queue=state.scheduling.tenant_queue,
            model_lane=state.scheduling.model_lane,
            local_queue=scheduling.resolved_local_queue,
            cluster_queue=scheduling.resolved_cluster_queue,
            workload_priority_class=scheduling.workload_priority_class,
            priority_value=scheduling.workload_priority_value,
            admission_state=cast(Any, admission_state),
            admission_reason=admission_reason,
            admitted_at=record.get("admitted_at"),
            queue_position=_unavailable("count", "Queue position is not measured by the controller."),
            shard_counts=_shard_counts(stage),
        ),
        fast_start=ScientificFastStartObservation(
            tier="not-observed",
            evidence="unavailable",
            observed_at=None,
            runtime_identity_digest=backend.execution_identity_digest,
            reason="No exact fast-start tier is joined to this controller attempt.",
        ),
        stage_counts=_stage_counts(state),
        gpu_accounting=_gpu_accounting(),
        error=_error(state),
        cancellation=ScientificCancellation(
            state="acknowledged"
            if state.status is BatchStatus.CANCELLED
            else "requested"
            if state.cancel_requested
            else "not-requested",
            requested_at=cancel_requested_at,
            requested_by=None if cancel_actor is None else _bounded(cancel_actor, 200, "unknown"),
            reason=("Cancellation was recorded in the append-only audit ledger." if state.cancel_requested else None),
            mode="terminate-attempt",
            grace_seconds=None,
            can_cancel=not terminal and not state.cancel_requested,
        ),
    )


def _attempt(
    attempt: ScientificAttemptState,
    events: tuple[BatchEvent, ...],
    *,
    resource_class: ResourceClass,
) -> ScientificAttempt:
    if attempt.outcome is AttemptOutcome.ACTIVE:
        status = (
            "queued"
            if attempt.last_phase
            in {LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING, LifecyclePhase.ADMITTED, LifecyclePhase.NODE_PENDING}
            else "running"
        )
    else:
        status = attempt.outcome.value
    admission = attempt.scheduling_admission
    gpu_count = (
        admission.accelerator_count if admission is not None else (0 if resource_class is ResourceClass.CPU else None)
    )
    failure = None
    phase_event = next((event for event in reversed(events) if event.draft.phase == attempt.last_phase), None)
    pending_reasons = {
        "UnschedulableInsufficientCpu": (
            "Waiting for a node: the selected pool cannot currently fit this Pod's full CPU request, "
            "including sidecars."
        ),
        "UnschedulableInsufficientMemory": (
            "Waiting for a node: insufficient allocatable memory for this Pod in the selected pool."
        ),
        "UnschedulableInsufficientGpu": (
            "Waiting for a node: the requested GPUs are not currently available in the selected pool."
        ),
        "UnschedulableNodeAffinity": (
            "Waiting for a matching node: the Pod's placement or affinity requirements are not satisfied."
        ),
        "NodeProvisioning": "Waiting for a schedulable node in the selected pool; GPU computation has not started.",
    }
    phase_reason = None
    if attempt.outcome is AttemptOutcome.ACTIVE and attempt.last_phase is LifecyclePhase.NODE_PENDING:
        phase_reason = pending_reasons.get(
            (phase_event.draft.code or "") if phase_event else "", pending_reasons["NodeProvisioning"]
        )
    if attempt.failure_code is not None:
        code = _bounded(attempt.failure_code, 64, "scientific_attempt_failed")
        failure = ScientificError(
            code=code,
            message=f"Scientific attempt failed with code {code}.",
            retryable=attempt.failure_kind.retryable if attempt.failure_kind is not None else False,
        )
    return ScientificAttempt(
        id=str(attempt.attempt_id),
        number=attempt.attempt_number,
        status=cast(Any, status),
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        workload_uid=attempt.kueue_workload_uid,
        job_uid=attempt.workload.uid if attempt.workload.kind is WorkloadKind.JOB else None,
        pod_count=len(attempt.pod_uids),
        node_count=None,
        gpu_count=gpu_count,
        admitted_at=admission.admitted_at if admission is not None else None,
        resolved_pool_id=admission.resolved_pool_id if admission is not None else None,
        admitted_resource_flavor=admission.admitted_resource_flavor if admission is not None else None,
        accelerator_resource_name=admission.accelerator_resource_name if admission is not None else None,
        checkpoint_input_artifact_id=None,
        checkpoint_output_artifact_id=None,
        error=failure,
        phase=attempt.last_phase.value,
        phase_reason=phase_reason,
        phase_observed_at=phase_event.occurred_at if phase_event is not None else None,
    )


def _stages(state: ScientificBatchState, events: tuple[BatchEvent, ...]) -> list[ScientificStage]:
    by_attempt: dict[UUID, list[BatchEvent]] = defaultdict(list)
    for event in events:
        if event.draft.attempt_id is not None:
            by_attempt[event.draft.attempt_id].append(event)
    result: list[ScientificStage] = []
    for ordinal, plan in enumerate(state.plan.stages, start=1):
        stage = state.stage(plan.stage_id)
        result.append(
            ScientificStage(
                id=plan.stage_id,
                display_name=plan.stage_id.replace("-", " ").title(),
                ordinal=ordinal,
                needs=list(plan.depends_on),
                resource_class="cpu" if plan.resource_class is ResourceClass.CPU else "gpu",
                admission_mode=plan.mode.value,
                checkpoint_mode=plan.checkpoint_mode.value,
                status=cast(Any, _stage_status(stage)),
                attempts=[
                    _attempt(
                        attempt,
                        tuple(by_attempt.get(attempt.attempt_id, ())),
                        resource_class=plan.resource_class,
                    )
                    for attempt in stage.attempts
                ],
            )
        )
    return result


class PostgresScientificRunAdminAdapter:
    """Bounded read-only projection over durable scientific controller state."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool[Any],
        batches: PostgresScientificBatchRepository,
        models: ScientificModelAdminAdapter,
        lifecycle_accounting: bool = False,
    ) -> None:
        self.pool = pool
        self.batches = batches
        self.models = models
        self.lifecycle_accounting = lifecycle_accounting

    async def _accounting(self, states: list[ScientificBatchState]) -> dict[UUID, ScientificGpuAccounting]:
        if not self.lifecycle_accounting or not states:
            return {}
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT subject.operation_id,subject.attempt_id,rollup.*
                   FROM fs2_telemetry_subjects subject
                   LEFT JOIN fs2_reporting_lifecycle_latest rollup USING(subject_id)
                   WHERE subject.operation_id=ANY($1::uuid[]) AND subject.workload_kind='scientific_batch'""",
                [state.operation_id for state in states],
            )
        grouped: dict[UUID, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["operation_id"]].append(row)
        return {
            state.operation_id: accounting for state in states
            if (accounting := project_gpu_accounting(state, grouped[state.operation_id])) is not None
        }

    async def _phase_times(self, state: ScientificBatchState) -> list[ScientificLifecyclePhase]:
        if not self.lifecycle_accounting:
            return project_phase_times(())
        expected = {attempt.attempt_id for stage in state.stages for attempt in stage.attempts}
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT subject.attempt_id,signal.*
                   FROM fs2_telemetry_subjects subject
                   JOIN fs2_lifecycle_signals signal USING(subject_id)
                   WHERE subject.operation_id=$1 AND subject.tenant_id=$2
                     AND subject.workload_kind='scientific_batch'
                     AND signal.clock IN ('phase','lifecycle')
                   ORDER BY signal.id LIMIT 100001""",
                state.operation_id, state.tenant_id,
            )
        # Match the existing lifecycle detail's bounded signal projection.
        if len(rows) > 100000:
            return project_phase_times(())
        selected = [row for row in rows if row["attempt_id"] in expected]
        return project_phase_times(
            tuple(_signal_from_row(row) for row in selected),
            incomplete_attempts={row["attempt_id"] for row in selected} != expected,
        )

    async def _model_map(self, *, tenant_id: str | None) -> dict[str, ScientificModelReadiness]:
        snapshot = await self.models.list_models(tenant_id=tenant_id)
        return {item.model_id: item for item in snapshot.data.items}

    @staticmethod
    def _base_select() -> str:
        return """
            SELECT operation.id,operation.tenant_id,operation.principal_id,
                   operation.model_id,operation.model_revision,operation.operation,operation.accepted_at,
                   operation.completed_at,token.prefix AS token_prefix,token.created_by,
                   batch.state,batch.updated_at,
                   (SELECT max(event.occurred_at) FROM fs2_scientific_batch_events event
                    WHERE event.operation_id=operation.id AND event.phase='admitted') AS admitted_at,
                   (SELECT audit.occurred_at FROM fs2_audit_events audit
                    WHERE audit.target_type='operation' AND audit.target_id=operation.id::text
                      AND audit.action='scientific_batch.cancel'
                    ORDER BY audit.occurred_at,audit.id LIMIT 1) AS cancel_requested_at,
                   (SELECT audit.actor FROM fs2_audit_events audit
                    WHERE audit.target_type='operation' AND audit.target_id=operation.id::text
                      AND audit.action='scientific_batch.cancel'
                    ORDER BY audit.occurred_at,audit.id LIMIT 1) AS cancel_actor,
                   CASE WHEN batch.status='queued' AND NOT batch.cancel_requested
                        THEN fs2_scientific_dispatch_hold(batch.model_id,batch.tenant_id) END AS dispatch_hold
            FROM fs2_scientific_batches batch
            JOIN fs2_operations operation ON operation.id=batch.operation_id
            JOIN fs2_tokens token ON token.id=operation.token_id
        """

    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        models = await self._model_map(tenant_id=query.tenant_id)
        args: list[object] = [query.from_at, query.to_at]
        clauses = ["operation.accepted_at >= $1", "operation.accepted_at < $2"]

        def bind(value: object) -> str:
            args.append(value)
            return f"${len(args)}"

        if query.tenant_id is not None:
            clauses.append(f"operation.tenant_id={bind(query.tenant_id)}")
        if query.model_id is not None:
            clauses.append(f"operation.model_id={bind(query.model_id)}")
        if query.service_class is not None:
            clauses.append(f"batch.state#>>'{{scheduling,service_class}}'={bind(query.service_class.value)}")
        if query.access_state is not None:
            model_ids = [item.model_id for item in models.values() if item.access.state == query.access_state]
            if not model_ids:
                return ScientificRunListSnapshot(
                    data=ScientificRunList(items=[], next_cursor=None),
                    observed_at=datetime.now(UTC),
                )
            clauses.append(f"operation.model_id=ANY({bind(model_ids)}::text[])")
        latest_phase = (
            "(SELECT e.phase FROM fs2_scientific_batch_events e "
            "WHERE e.operation_id=operation.id AND e.kind='lifecycle' "
            "ORDER BY e.sequence DESC LIMIT 1)"
        )
        admitted = (
            f"{latest_phase} IN ('admitted','node_pending','image_loading','artifact_loading',"
            "'restoring','semantic_warmup','active_compute','allocated_idle','grace_drain','teardown')"
        )
        terminal = "batch.status IN ('succeeded','failed','cancelled')"
        if query.run_status == "waiting-for-access":
            return ScientificRunListSnapshot(data=ScientificRunList(items=[]), observed_at=datetime.now(UTC))
        if query.run_status == "queued":
            clauses.extend(["batch.status='queued'", "NOT batch.cancel_requested", f"NOT {admitted}"])
        elif query.run_status == "admitted":
            clauses.extend(["batch.status='queued'", "NOT batch.cancel_requested", admitted])
        elif query.run_status == "running":
            clauses.extend(["batch.status='running'", "NOT batch.cancel_requested"])
        elif query.run_status == "cancelling":
            clauses.extend(["batch.cancel_requested", f"NOT {terminal}"])
        elif query.run_status in {"succeeded", "failed", "cancelled"}:
            clauses.append(f"batch.status={bind(query.run_status)}")
        if query.admission_state == "pending":
            clauses.extend([f"NOT {terminal}", f"NOT {admitted}"])
        elif query.admission_state == "admitted":
            clauses.extend([f"NOT {terminal}", admitted])
        elif query.admission_state == "finished":
            clauses.append(terminal)
        elif query.admission_state == "evicted":
            clauses.append(f"{latest_phase}='preempted'")
        elif query.admission_state == "inadmissible":
            return ScientificRunListSnapshot(data=ScientificRunList(items=[]), observed_at=datetime.now(UTC))
        if query.cursor is not None:
            accepted_at, operation_id = _decode_cursor(query.cursor)
            clauses.append(f"(operation.accepted_at,operation.id)<({bind(accepted_at)},{bind(operation_id)})")
        args.append(query.limit + 1)
        sql = (
            self._base_select()
            + " WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY operation.accepted_at DESC,operation.id DESC LIMIT ${len(args)}"
        )
        async with self.pool.acquire() as connection:
            observed_at = await connection.fetchval("SELECT clock_timestamp()")
            records = await connection.fetch(sql, *args)
        page = records[: query.limit]
        states = [state_from_value(record["state"]) for record in page]
        accounting = await self._accounting(states)
        try:
            items = [
                _summary(cast(Mapping[str, Any], record), state, models[record["model_id"]])
                for record, state in zip(page, states, strict=True)
            ]
        except KeyError as error:
            raise ScientificAdminSourceUnavailableError("scientific run model identity is absent") from error
        items = [
            item.model_copy(update={"gpu_accounting": accounting[UUID(item.id)]})
            if UUID(item.id) in accounting
            else item
            for item in items
        ]
        next_cursor = None
        if len(records) > query.limit and page:
            next_cursor = _encode_cursor(page[-1]["accepted_at"], page[-1]["id"])
        return ScientificRunListSnapshot(
            data=ScientificRunList(items=items, next_cursor=next_cursor),
            observed_at=observed_at,
        )

    async def get_run(self, operation_id: UUID, *, tenant_id: str | None) -> ScientificRunDetailSnapshot:
        args: list[object] = [operation_id]
        tenant_clause = ""
        if tenant_id is not None:
            args.append(tenant_id)
            tenant_clause = " AND operation.tenant_id=$2"
        async with self.pool.acquire() as connection:
            observed_at = await connection.fetchval("SELECT clock_timestamp()")
            record = await connection.fetchrow(
                self._base_select() + " WHERE operation.id=$1" + tenant_clause,
                *args,
            )
        if record is None:
            raise KeyError(operation_id)
        state = state_from_value(record["state"])
        models = await self._model_map(tenant_id=tenant_id)
        try:
            model = models[state.model_id]
        except KeyError as error:
            raise ScientificAdminSourceUnavailableError("scientific run model identity is absent") from error
        events = tuple(await self.batches.list_events(operation_id, tenant_id=state.tenant_id, limit=1000))
        max_attempts = max(stage.max_attempts for stage in state.plan.stages)
        detail = ScientificRunDetail(
            run=_summary(cast(Mapping[str, Any], record), state, model),
            lifecycle_phases=await self._phase_times(state),
            stages=_stages(state, events),
            artifacts=[],
            retry=ScientificRetry(max_attempts_per_stage=max_attempts, retryable_exit_codes=[]),
            semantic_validation=ScientificSemanticValidation(
                validator_id="unavailable",
                status="not-run",
                receipt_digest=None,
            ),
            observability=[
                ScientificObservabilityLink(
                    kind=cast(Any, kind),
                    label=label,
                    available=True,
                    href=f"/admin/observability?operation_id={operation_id}&signal={kind}",
                    reason=None,
                )
                for kind, label in (("trace", "Request trace"), ("logs", "Workload logs"), ("metrics", "GPU metrics"))
            ],
        )
        accounting = await self._accounting([state])
        if operation_id in accounting:
            detail = detail.model_copy(
                update={"run": detail.run.model_copy(update={"gpu_accounting": accounting[operation_id]})}
            )
        return ScientificRunDetailSnapshot(data=detail, observed_at=observed_at)


class PostgresScientificRunControlAdapter:
    """Record an operator cancel request through the controller's own repository.

    The repository write is the same transaction the public ``:cancel`` route
    uses, so the controller observes it identically and the append-only audit
    ledger names the operator subject as the actor.
    """

    def __init__(self, *, batches: PostgresScientificBatchRepository) -> None:
        self.batches = batches

    async def request_cancel(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        actor: str,
    ) -> ScientificRunCancelOutcome:
        try:
            before = await self.batches.get(operation_id, tenant_id=tenant_id)
        except ScientificBatchNotFoundError as error:
            raise KeyError(operation_id) from error
        if before.status.terminal:
            return "terminal"
        if before.cancel_requested:
            return "already-requested"
        after = await self.batches.request_cancel(operation_id, tenant_id=tenant_id, actor=actor)
        # The repository leaves the row untouched when the batch turned terminal
        # between the read and the locked update; report that truthfully.
        return "requested" if after.cancel_requested else "terminal"


def _policy_setting(
    record: ScientificModelPolicyRecord | None,
    *,
    tenant_id: str | None,
) -> ScientificModelPolicySetting:
    if record is None:
        return ScientificModelPolicySetting(tenant_id=tenant_id, revision=0)
    return ScientificModelPolicySetting(
        tenant_id=record.tenant_id,
        revision=record.revision,
        paused=record.paused,
        max_active_runs=record.max_active_runs,
        startup_policies={
            stage: ScientificStageStartupPolicy.model_validate(choice)
            for stage, choice in record.startup_policies.items()
        },
        reason=record.reason,
        updated_by=_bounded(record.updated_by, 200, "unknown"),
        updated_at=record.updated_at,
    )


def _dispatch_state(view: ScientificModelPolicyView) -> ScientificDispatchStateModel:
    scope = "all tenants" if view.tenant_id is None else f"tenant {view.tenant_id}"
    state = view.dispatch_state
    limit = view.effective_max_active_runs
    if state == "paused":
        source = view.policy if view.policy is not None and view.policy.paused else view.inherited
        by = "this tenant's policy" if source is not None and source.tenant_id is not None else "the all-tenants policy"
        reason = f"New dispatch for {scope} is paused by {by}; running work drains and results still publish."
        if source is not None and source.reason:
            reason = f"{reason} Operator note: {source.reason}"
    elif state == "at-limit":
        running = view.counts.running if view.tenant_id is not None else view.all_tenants_counts.running
        reason = (
            f"Dispatch for {scope} is held: {running} active run(s) meet the cap of {limit}; "
            f"{view.counts.queued} queued run(s) wait durably in priority order."
        )
    elif limit is not None:
        reason = (
            f"Dispatch for {scope} is open below a cap of {limit} active run(s); "
            f"{view.counts.running} running, {view.counts.queued} queued."
        )
    else:
        reason = (
            f"Dispatch for {scope} is open with no operator cap; Kueue quota remains the ceiling. "
            f"{view.counts.running} running, {view.counts.queued} queued."
        )
    return ScientificDispatchStateModel(
        state=cast(Any, state),
        paused=state == "paused",
        max_active_runs=limit,
        reason=_bounded(reason, 300, "Dispatch state is unavailable."),
    )


def _policy(
    view: ScientificModelPolicyView,
    *,
    catalog_known: bool,
    startup_options: dict[str, list[str]] | None = None,
) -> ScientificModelPolicy:
    return ScientificModelPolicy(
        model_id=view.model_id,
        scope_tenant_id=view.tenant_id,
        catalog_known=catalog_known,
        startup_options=startup_options or {},
        desired=_policy_setting(view.policy, tenant_id=view.tenant_id),
        inherited=None if view.tenant_id is None else _policy_setting(view.inherited, tenant_id=None),
        effective=_dispatch_state(view),
        counts=ScientificDispatchCountsModel(queued=view.counts.queued, running=view.counts.running),
        all_tenants_counts=ScientificDispatchCountsModel(
            queued=view.all_tenants_counts.queued,
            running=view.all_tenants_counts.running,
        ),
    )


class PostgresScientificModelPolicyAdminAdapter:
    """Project and write the durable per-model dispatch policy.

    Counts and the effective decision come from the controller's own tables
    through the same SQL predicate the claim query and fenced transition use,
    so what the console shows is what the next poll will do.
    """

    def __init__(
        self,
        *,
        repository: PostgresScientificModelPolicyRepository,
        startup_validator: Callable[..., Any] | None = None,
        startup_options: Callable[..., dict[str, list[str]]] | None = None,
    ) -> None:
        self.repository = repository
        self.startup_validator = startup_validator
        self.startup_options = startup_options

    def _project(self, view: ScientificModelPolicyView, *, catalog_known: bool) -> ScientificModelPolicy:
        options = self.startup_options(model_id=view.model_id) if self.startup_options else {}
        return _policy(view, catalog_known=catalog_known, startup_options=options)

    async def list_policies(
        self,
        *,
        tenant_id: str | None,
        model_ids: tuple[str, ...],
    ) -> ScientificModelPolicySnapshot:
        known = set(model_ids)
        views = await self.repository.list(tenant_id=tenant_id, model_ids=model_ids)
        observed_at = datetime.now(UTC)
        return ScientificModelPolicySnapshot(
            data=ScientificModelPolicyList(
                scope_tenant_id=tenant_id,
                items=[self._project(view, catalog_known=view.model_id in known) for view in views[:256]],
            ),
            observed_at=observed_at,
        )

    async def set_policy(
        self,
        model_id: str,
        *,
        tenant_id: str | None,
        update: ScientificModelPolicyUpdate,
        actor: str,
    ) -> ScientificModelPolicy:
        startup = (
            None
            if update.startup_policies is None
            else {stage: policy.model_dump() for stage, policy in update.startup_policies.items()}
        )
        if startup:
            if self.startup_validator is None:
                raise ScientificModelPolicyInvalidError("scientific startup selection is not configured")
            try:
                startup = self.startup_validator(model_id=model_id, overrides=startup)
            except ValueError as error:
                raise ScientificModelPolicyInvalidError(str(error)) from None
        try:
            view = await self.repository.set(
                model_id,
                tenant_id=tenant_id,
                expected_revision=update.expected_revision,
                paused=update.paused,
                max_active_runs=update.max_active_runs,
                reason=update.reason,
                actor=actor,
                startup_policies=startup,
            )
        except ScientificModelPolicyStaleError as error:
            raise ScientificModelPolicyStaleRevisionError(error.current_revision) from None
        return self._project(view, catalog_known=True)


def _artifact_name(artifact: ArtifactRef, role: str) -> str:
    suffix = {
        "application/json": "json",
        "chemical/x-mmcif": "cif",
        "chemical/x-pdb": "pdb",
    }.get(artifact.media_type, "artifact")
    return f"{role}-{str(artifact.artifact_id)[:8]}.{suffix}"


class PostgresScientificArtifactAdminAdapter:
    """Project immutable artifact manifests without issuing signed handles."""

    def __init__(self, service: ScientificArtifactControllerPort | None) -> None:
        self.service = service

    @staticmethod
    def _artifact(
        artifact: ArtifactRef,
        *,
        operation_id: UUID,
        role: str,
        semantic_type: str,
        created_at: datetime,
    ) -> ScientificArtifact:
        return ScientificArtifact(
            artifact_id=artifact.artifact_id,
            name=_artifact_name(artifact, role),
            role=cast(Any, role),
            semantic_type=semantic_type,
            state="available",
            sha256=f"sha256:{artifact.sha256}",
            size_bytes=ScientificMeasurement(
                value=float(artifact.size_bytes),
                unit="bytes",
                evidence=ScientificEvidenceState.MEASURED,
                source="canonical-run-result",
            ),
            media_type=artifact.media_type,
            created_at=created_at,
            download=ScientificArtifactDownload(
                available=True,
                href=f"/admin/api/v1/scientific-runs/{operation_id}/artifacts/{artifact.artifact_id}/content",
                reason=None,
            ),
        )

    async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot:
        if self.service is None:
            raise ScientificAdminSourceUnavailableError("scientific artifact service is disabled")
        try:
            record = await self.service.get_run_result(operation_id, tenant_id=tenant_id)
        except ArtifactNotFoundError:
            return ScientificArtifactSnapshot(
                artifacts=(),
                semantic_validation=ScientificSemanticValidation(
                    validator_id="unavailable",
                    status="not-run",
                    receipt_digest=None,
                ),
                observed_at=datetime.now(UTC),
            )
        result = record.result
        artifacts_by_id: dict[str, ScientificArtifact] = {
            result.input_manifest.artifact_id: self._artifact(
                result.input_manifest,
                operation_id=operation_id,
                role="manifest",
                semantic_type="scientific-input-manifest",
                created_at=result.submitted_at,
            )
        }
        if result.output_manifest is not None:
            artifacts_by_id[result.output_manifest.artifact_id] = self._artifact(
                result.output_manifest,
                operation_id=operation_id,
                role="manifest",
                semantic_type="scientific-output-manifest",
                created_at=result.completed_at,
            )
        attempt_evidence = []
        for attempt in result.attempts:
            for checkpoint, semantic_type in (
                (attempt.checkpoint_input, "scientific-checkpoint-input"),
                (attempt.checkpoint_output, "scientific-checkpoint-output"),
            ):
                if checkpoint is not None:
                    artifacts_by_id[checkpoint.artifact_id] = self._artifact(
                        checkpoint,
                        operation_id=operation_id,
                        role="checkpoint",
                        semantic_type=semantic_type,
                        created_at=attempt.completed_at,
                    )
            admission = attempt.scheduling_admission
            attempt_evidence.append(
                ScientificArtifactAttemptEvidence(
                    attempt_id=attempt.attempt_id,
                    status=attempt.status.value,
                    started_at=attempt.started_at,
                    completed_at=attempt.completed_at,
                    workload_uid=attempt.kueue_workload_uid,
                    job_uid=attempt.k8s_job_uid,
                    pod_count=len(attempt.pod_uids),
                    node_count=len(attempt.node_uids),
                    gpu_count=admission.accelerator_count if admission is not None else None,
                    checkpoint_input_artifact_id=(
                        attempt.checkpoint_input.artifact_id if attempt.checkpoint_input is not None else None
                    ),
                    checkpoint_output_artifact_id=(
                        attempt.checkpoint_output.artifact_id if attempt.checkpoint_output is not None else None
                    ),
                    admitted_at=admission.admitted_at if admission is not None else None,
                    resolved_pool_id=admission.resolved_pool_id if admission is not None else None,
                    admitted_resource_flavor=(admission.admitted_resource_flavor if admission is not None else None),
                    accelerator_resource_name=(admission.accelerator_resource_name if admission is not None else None),
                )
            )
        # Include the actual stored output files, not only their JSON manifests.
        # These are verified, tenant/operation-bound records; no signed handle or
        # storage location is returned. Canonical manifest names retain priority.
        for stored in await self.service.list_artifacts(operation_id, tenant_id=tenant_id):
            artifact_id = str(stored.artifact_id)
            if artifact_id not in artifacts_by_id:
                projected = self._artifact(
                    stored.to_public_ref(),
                    operation_id=operation_id,
                    role=stored.direction.value,
                    semantic_type=f"{stored.stage_id}-artifact",
                    created_at=stored.created_at,
                )
                artifacts_by_id[artifact_id] = projected.model_copy(
                    update={"name": f"{stored.stage_id}-{projected.name}"[:256]}
                )
        access = result.access_admission
        error = result.error
        return ScientificArtifactSnapshot(
            artifacts=tuple(artifacts_by_id.values()),
            semantic_validation=ScientificSemanticValidation(
                validator_id=result.semantic_validation.validator_id,
                status=result.semantic_validation.status.value,
                receipt_digest=(
                    f"sha256:{result.semantic_validation.receipt_digest}"
                    if result.semantic_validation.receipt_digest is not None
                    else None
                ),
            ),
            # Commit time remains on the immutable result and artifact rows.
            # Freshness describes this successful read, not the result's age.
            observed_at=datetime.now(UTC),
            terminal_status=result.terminal_status.value,
            completed_at=result.completed_at,
            model_revision=result.execution_identity.model_revision,
            runtime_image_digest=result.execution_identity.runtime_image_digest,
            execution_identity_digest=result.execution_identity.execution_identity_sha256,
            access_profile=access.profile.value,
            access_state=access.state.value,
            access_receipt_digest=(f"sha256:{access.receipt_digest}" if access.receipt_digest is not None else None),
            service_class=ScientificServiceClass(result.scheduling_snapshot.service_class.value),
            attempts=tuple(attempt_evidence),
            error=(
                ScientificError(
                    code=error.code,
                    message=_bounded(error.message, 300, "Scientific run failed."),
                    retryable=error.retryable,
                )
                if error is not None
                else None
            ),
        )


def postgres_scientific_admin_read_service(
    *,
    pool: asyncpg.Pool[Any],
    registry: Registry,
    catalog_dir: Path,
    artifact_service: ScientificArtifactControllerPort | None,
    scientific_batches: ScientificBatchService | None,
    source_max_age_seconds: float,
    adapter_timeout_seconds: float,
) -> ScientificAdminReadService:
    """Build the production admin service over canonical durable sources."""

    run_models = ScientificCatalogFileAdapter(
        registry=registry,
        receipts_file=scientific_receipts_file(catalog_dir),
    )
    models = ScientificProfileDiscoveryAdapter(
        scientific_batches=scientific_batches,
        global_catalog=run_models,
    )
    batches = PostgresScientificBatchRepository(pool)
    renderer = getattr(scientific_batches, "execution_binding", None)
    return ScientificAdminReadService(
        runs=PostgresScientificRunAdminAdapter(
            pool=pool, batches=batches, models=run_models, lifecycle_accounting=True
        ),
        artifacts=(PostgresScientificArtifactAdminAdapter(artifact_service) if artifact_service is not None else None),
        controls=PostgresScientificRunControlAdapter(batches=batches),
        policies=PostgresScientificModelPolicyAdminAdapter(
            repository=PostgresScientificModelPolicyRepository(pool),
            startup_validator=getattr(renderer, "validate_startup_policy_overrides", None),
            startup_options=getattr(renderer, "startup_policy_options", None),
        ),
        models=models,
        source_max_age_seconds=source_max_age_seconds,
        adapter_timeout_seconds=adapter_timeout_seconds,
    )
