"""PostgreSQL-backed scientific admin projections.

The adapter reads only bounded, payload-free controller and operation fields.
It intentionally leaves GPU accounting unavailable until the exact lifecycle
ledger publishes allocation identities and closed GPU intervals.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import asyncpg

from .registry import Registry
from .scientific_admin import (
    ScientificAdminQueryError,
    ScientificAdminReadService,
    ScientificAdminSourceUnavailableError,
    ScientificArtifactAttemptEvidence,
    ScientificArtifactSnapshot,
    ScientificModelAdminAdapter,
    ScientificRunDetailSnapshot,
    ScientificRunListSnapshot,
    ScientificRunQuery,
)
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
)
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
from .scientific_batch.postgres_repository import PostgresScientificBatchRepository
from .scientific_batch.service import ScientificBatchService
from .scientific_run_result import ArtifactRef

_PHASES = {
    LifecyclePhase.QUEUED: "queue",
    LifecyclePhase.SCHEDULING: "admission",
    LifecyclePhase.ADMITTED: "admission",
    LifecyclePhase.NODE_PENDING: "admission",
    LifecyclePhase.IMAGE_LOADING: "image-pull",
    LifecyclePhase.ARTIFACT_LOADING: "artifact-load",
    LifecyclePhase.RESTORING: "restore",
    LifecyclePhase.SEMANTIC_WARMUP: "semantic-warmup",
    LifecyclePhase.ACTIVE_COMPUTE: "active-compute",
    LifecyclePhase.ALLOCATED_IDLE: "allocated-idle",
    LifecyclePhase.GRACE_DRAIN: "grace-drain",
    LifecyclePhase.TEARDOWN: "teardown",
}
_PHASE_ORDER = tuple(dict.fromkeys(_PHASES.values()))
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
    latest = stage.attempts[-1]
    if latest.last_phase in {LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING}:
        return "queued"
    if latest.last_phase is LifecyclePhase.ADMITTED:
        return "admitted"
    return "running"


def _run_status(state: ScientificBatchState) -> str:
    if state.cancel_requested and not state.status.terminal:
        return "cancelling"
    if state.status is BatchStatus.SUCCEEDED:
        return "succeeded"
    if state.status is BatchStatus.FAILED:
        return "failed"
    if state.status is BatchStatus.CANCELLED:
        return "cancelled"
    if state.status is BatchStatus.RUNNING:
        return "running"
    if any(_stage_status(stage) == "admitted" for stage in state.stages):
        return "admitted"
    return "queued"


def _active_stage(state: ScientificBatchState) -> ScientificStageState:
    return next(
        (stage for stage in state.stages if stage.status is StageStatus.ACTIVE),
        next((stage for stage in state.stages if not stage.status.terminal), state.stages[-1]),
    )


def _admission_state(state: ScientificBatchState) -> tuple[str, str]:
    if state.status in _TERMINAL_BATCH_STATUS:
        return "finished", "The durable scientific batch is terminal."
    stage = _active_stage(state)
    latest = stage.attempts[-1] if stage.attempts else None
    if latest is not None and latest.outcome is AttemptOutcome.PREEMPTED:
        return "evicted", "The latest attempt was preempted and awaits controller reconciliation."
    if latest is not None and not latest.resource_released and latest.last_phase.rank >= LifecyclePhase.ADMITTED.rank:
        return "admitted", "The admitted workload has not yet reached its UID-confirmed release boundary."
    return "pending", "The controller has not observed a Kueue admission event."


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
    stage = _active_stage(state)
    scheduling = state.scheduling.stage(stage.stage_id)
    admission_state, admission_reason = _admission_state(state)
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
            execution_mode=model.execution_mode,
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
    del events
    if attempt.outcome is AttemptOutcome.ACTIVE:
        status = "queued" if attempt.last_phase in {LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING} else "running"
    else:
        status = attempt.outcome.value
    admission = attempt.scheduling_admission
    gpu_count = (
        admission.accelerator_count if admission is not None else (0 if resource_class is ResourceClass.CPU else None)
    )
    failure = None
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


def _lifecycle(events: tuple[BatchEvent, ...]) -> list[ScientificLifecyclePhase]:
    by_attempt: dict[UUID, list[BatchEvent]] = defaultdict(list)
    for event in events:
        if event.draft.attempt_id is not None and event.draft.phase is not None:
            by_attempt[event.draft.attempt_id].append(event)
    totals: dict[str, float] = defaultdict(float)
    for attempt_events in by_attempt.values():
        ordered = sorted(attempt_events, key=lambda item: item.sequence)
        for current, following in zip(ordered, ordered[1:], strict=False):
            phase = _PHASES.get(current.draft.phase) if current.draft.phase is not None else None
            if phase is not None:
                totals[phase] += max(0.0, (following.occurred_at - current.occurred_at).total_seconds())
    return [
        ScientificLifecyclePhase(
            phase=cast(Any, phase),
            duration=(
                ScientificMeasurement(
                    value=totals[phase],
                    unit="seconds",
                    evidence=ScientificEvidenceState.MEASURED,
                    source="scientific-controller-events",
                )
                if phase in totals
                else ScientificMeasurement(
                    value=None,
                    unit="seconds",
                    evidence=ScientificEvidenceState.UNAVAILABLE,
                    source="scientific-controller-events",
                    reason="No closed controller interval is available for this phase.",
                )
            ),
        )
        for phase in _PHASE_ORDER
    ]


class PostgresScientificRunAdminAdapter:
    """Bounded read-only projection over durable scientific controller state."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool[Any],
        batches: PostgresScientificBatchRepository,
        models: ScientificModelAdminAdapter,
    ) -> None:
        self.pool = pool
        self.batches = batches
        self.models = models

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
                    ORDER BY audit.occurred_at,audit.id LIMIT 1) AS cancel_actor
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
        try:
            items = [
                _summary(cast(Mapping[str, Any], record), state_from_value(record["state"]), models[record["model_id"]])
                for record in page
            ]
        except KeyError as error:
            raise ScientificAdminSourceUnavailableError("scientific run model identity is absent") from error
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
            lifecycle_phases=_lifecycle(events),
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
        return ScientificRunDetailSnapshot(data=detail, observed_at=observed_at)


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
                available=False,
                href=None,
                reason="Use the authorized artifact endpoint to request a short-lived download handle.",
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
                role="manifest",
                semantic_type="scientific-input-manifest",
                created_at=result.submitted_at,
            )
        }
        if result.output_manifest is not None:
            artifacts_by_id[result.output_manifest.artifact_id] = self._artifact(
                result.output_manifest,
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
            observed_at=record.committed_at,
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

    models = ScientificProfileDiscoveryAdapter(scientific_batches=scientific_batches)
    run_models = ScientificCatalogFileAdapter(
        registry=registry,
        receipts_file=scientific_receipts_file(catalog_dir),
    )
    batches = PostgresScientificBatchRepository(pool)
    return ScientificAdminReadService(
        runs=PostgresScientificRunAdminAdapter(pool=pool, batches=batches, models=run_models),
        artifacts=PostgresScientificArtifactAdminAdapter(artifact_service),
        models=models,
        source_max_age_seconds=source_max_age_seconds,
        adapter_timeout_seconds=adapter_timeout_seconds,
    )
