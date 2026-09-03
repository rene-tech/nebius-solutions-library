"""Authorized API/MCP consumer over durable Operations and controller state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import Field

from ..auth import require_operation_access
from ..models import AdmissionRequest, OperationView, PendingScientificAdmission, Principal, Scope, StrictModel
from ..scientific_run_result import ArtifactRef, ScientificRunResult
from ..scientific_run_result import SchedulingAdmission as PublicSchedulingAdmission
from ..store import ConflictError, Store
from .catalog_adapter import CatalogProfileAdapterError, scientific_plan_from_catalog_profile
from .codec import state_from_value, state_to_value
from .controller import ScientificBatchController
from .models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    AttemptOutcome,
    BatchEvent,
    BatchEventKind,
    BatchStatus,
    FailureKind,
    LifecyclePhase,
    RuntimeArtifactLocalization,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificInputAdmission,
    ScientificInputArtifact,
    ServiceClass,
    StageStatus,
    WorkloadKind,
)
from .postgres_repository import ScientificBatchNotFoundError
from .profile_catalog import ScientificProfileCatalog, ScientificProfileError, ScientificWorkloadProfile
from .protocols import BatchRepositoryConflictError
from .scheduling import SchedulingContractError, SchedulingContractResolver


class ScientificArtifactAccess(Protocol):
    """Consumer seam implemented by the artifact-service owner."""

    async def validate_input(self, pointer: Mapping[str, Any], *, tenant_id: str) -> ScientificInputAdmission: ...

    async def artifact_response(self, artifact_id: UUID, *, tenant_id: str) -> Mapping[str, Any]: ...

    async def result_response(self, operation_id: UUID, *, tenant_id: str) -> Mapping[str, Any]: ...


class ScientificPlanFactory(Protocol):
    def plan(
        self,
        profile: ScientificWorkloadProfile,
        request: Mapping[str, Any],
        *,
        operation_id: UUID | None = None,
        access_context: ArtifactAccessContext,
        input_artifacts: tuple[ScientificInputArtifact, ...],
    ) -> ScientificBatchPlan | AdapterExecutionPlan: ...


class ScientificExecutionBinding(Protocol):
    """Operator-owned binding kept outside the canonical public profile schema."""

    execution_map_sha256: str

    def access_context(self, profile: ScientificWorkloadProfile, *, tenant_id: str) -> ArtifactAccessContext: ...

    def variant_id(self, model_id: str) -> str: ...

    def workload_namespace(self, model_id: str) -> str: ...

    def collector_id(self, model_id: str, stage_id: str) -> str: ...

    def verify_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        execution_plan: AdapterExecutionPlan,
        access_context: ArtifactAccessContext,
    ) -> tuple[RuntimeArtifactLocalization, ...]: ...

    def bind_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        execution_plan: AdapterExecutionPlan,
        access_context: ArtifactAccessContext,
        localizations: tuple[RuntimeArtifactLocalization, ...],
    ) -> AdapterExecutionPlan: ...


class CatalogScientificPlanFactory:
    """Default adapter for profiles whose catalog minimum expands to one unit."""

    def plan(
        self,
        profile: ScientificWorkloadProfile,
        request: Mapping[str, Any],
        *,
        operation_id: UUID | None = None,
        access_context: ArtifactAccessContext,
        input_artifacts: tuple[ScientificInputArtifact, ...],
    ) -> ScientificBatchPlan:
        del request, operation_id, access_context, input_artifacts
        return scientific_plan_from_catalog_profile(profile.value)


class ScientificBatchServiceRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...

    async def request_cancel(self, operation_id: UUID, *, tenant_id: str, actor: str) -> ScientificBatchState: ...

    async def list_events(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[BatchEvent]: ...


class ScientificAttemptView(StrictModel):
    """Closed operational attempt view; actual admission reuses the public result shape."""

    attempt_id: UUID
    shard_id: str | None
    attempt_number: int = Field(ge=1, le=10)
    workload_kind: WorkloadKind
    workload_name: str
    workload_uid: str | None
    workload_namespace: str
    route_namespace: str
    outcome: AttemptOutcome
    last_phase: LifecyclePhase
    resource_released: bool
    scheduling_admission: PublicSchedulingAdmission | None
    failure_kind: FailureKind | None
    failure_code: str | None


class ScientificStageView(StrictModel):
    stage_id: str
    status: StageStatus
    failure_code: str | None
    attempts: tuple[ScientificAttemptView, ...] = Field(max_length=10_240)


class ScientificBatchView(StrictModel):
    batch_id: UUID
    workload_id: UUID
    model_id: str
    variant_id: str
    input_artifact_id: UUID
    status: BatchStatus
    revision: int = Field(ge=0)
    cancel_requested: bool
    failure_code: str | None
    result_published: bool
    scheduling_snapshot_digest: str
    service_class: ServiceClass
    workload_namespace: str
    route_namespace: str
    stages: tuple[ScientificStageView, ...] = Field(min_length=1, max_length=64)


class ScientificBatchStatusResponse(StrictModel):
    operation: OperationView
    batch: ScientificBatchView


class ScientificEventView(StrictModel):
    sequence: int = Field(ge=1)
    event_id: str
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    kind: BatchEventKind
    stage_id: str | None
    shard_id: str | None
    attempt_id: UUID | None
    phase: LifecyclePhase | None
    code: str | None
    occurred_at: datetime


class ScientificEventPage(StrictModel):
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    model_id: str
    variant_id: str
    data: tuple[ScientificEventView, ...] = Field(max_length=1000)


class ScientificProfileDiscovery(StrictModel):
    """A profile that passed the same static gates required by submission."""

    model_id: str
    display_name: str
    execution_mode: str
    operations: tuple[str, ...]
    service_classes: tuple[str, ...]
    parameter_schema: str
    source_repository: str
    source_revision: str
    variant_id: str
    runtime_image_digest: str
    execution_identity_sha256: str
    access_profile: str
    access_state: str
    access_receipt_digest: str | None
    h100_semantic_receipt_sha256: str
    public_completion_receipt_sha256: str
    scheduler_eligibility_receipt_sha256: str
    execution_map_sha256: str
    qualified_at: str
    mcp_tool_name: str
    mcp_description: str


class ScientificBatchService:
    def __init__(
        self,
        *,
        store: Store,
        repository: ScientificBatchServiceRepository,
        controller: ScientificBatchController,
        profiles: ScientificProfileCatalog,
        scheduling: SchedulingContractResolver,
        artifacts: ScientificArtifactAccess,
        execution_binding: ScientificExecutionBinding,
        plan_factory: ScientificPlanFactory | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.controller = controller
        self.profiles = profiles
        self.scheduling = scheduling
        self.artifacts = artifacts
        self.execution_binding = execution_binding
        self.plan_factory = plan_factory or CatalogScientificPlanFactory()

    @staticmethod
    def _authorize(principal: Principal, scope: Scope, *, model_id: str | None = None) -> None:
        principal.require(scope, model_id=model_id)

    def discovery_profiles(
        self,
        *,
        tenant_id: str | None,
        allowed_models: frozenset[str],
        surface: str,
    ) -> tuple[ScientificProfileDiscovery, ...]:
        """Return only profiles with a complete tenant-specific admission path.

        This is deliberately a fail-closed projection.  It performs no artifact
        read or durable admission, but it verifies the exact runtime binding and
        asks the authoritative scheduler to freeze every advertised service
        class using the profile's minimum legal plan.
        """

        if tenant_id is None or surface not in {"admin", "mcp"}:
            return ()
        discovered: list[ScientificProfileDiscovery] = []
        for profile in self.profiles.list():
            if "*" not in allowed_models and profile.model_id not in allowed_models:
                continue
            if surface == "mcp" and not (profile.mcp_discoverable and profile.mcp_invocable):
                continue
            try:
                qualification = profile.value["qualification"]
                if self.execution_binding.execution_map_sha256 != (
                    f"sha256:{qualification['execution_map_sha256']}"
                ):
                    raise ScientificProfileError("scientific qualification binds another execution map")
                self.execution_binding.access_context(profile, tenant_id=tenant_id)
                variant_id = self.execution_binding.variant_id(profile.model_id)
                workload_namespace = self.execution_binding.workload_namespace(profile.model_id)
                for stage in profile.value["workload"]["stages"]:
                    self.execution_binding.collector_id(profile.model_id, str(stage["id"]))
                plan = scientific_plan_from_catalog_profile(profile.value)
                possible_attempts = sum(len(stage.workload_units) * stage.max_attempts for stage in plan.stages)
                if possible_attempts > self.profiles.max_result_attempts:
                    raise ScientificProfileError("scientific plan exceeds the public result attempt bound")
                for service_class in profile.service_classes:
                    self.scheduling.freeze(
                        service_class=service_class,
                        model_id=profile.model_id,
                        tenant_id=tenant_id,
                        profile=profile.value,
                        plan=plan,
                        workload_namespace=workload_namespace,
                        captured_at=datetime(1970, 1, 1, tzinfo=UTC),
                    )
            except (
                AttributeError,
                CatalogProfileAdapterError,
                KeyError,
                SchedulingContractError,
                ScientificProfileError,
                TypeError,
            ):
                continue
            discovered.append(
                ScientificProfileDiscovery(
                    model_id=profile.model_id,
                    display_name=profile.display_name,
                    execution_mode=profile.execution_mode,
                    operations=profile.operations,
                    service_classes=profile.service_classes,
                    parameter_schema=profile.parameter_schema,
                    source_repository=profile.source_repository,
                    source_revision=profile.model_revision,
                    variant_id=variant_id,
                    runtime_image_digest=profile.runtime_image_digest,
                    execution_identity_sha256=profile.execution_identity_sha256,
                    access_profile=profile.access_profile,
                    access_state=profile.access_state,
                    access_receipt_digest=profile.access_receipt_digest,
                    h100_semantic_receipt_sha256=str(qualification["h100_semantic_receipt_sha256"]),
                    public_completion_receipt_sha256=str(qualification["public_completion_receipt_sha256"]),
                    scheduler_eligibility_receipt_sha256=str(
                        qualification["scheduler_eligibility_receipt_sha256"]
                    ),
                    execution_map_sha256=str(qualification["execution_map_sha256"]),
                    qualified_at=str(qualification["qualified_at"]),
                    mcp_tool_name=profile.mcp_tool_name,
                    mcp_description=profile.mcp_description,
                )
            )
        return tuple(discovered)

    def discover(self, principal: Principal, *, surface: str) -> dict[str, Any]:
        """Authorized public discovery used by MCP and equivalent callers."""

        self._authorize(principal, Scope.CATALOG_READ)
        if Scope.INFERENCE_INVOKE.value not in principal.scopes:
            return {"object": "list", "data": []}
        profiles = self.discovery_profiles(
            tenant_id=principal.tenant_id,
            allowed_models=frozenset(principal.models),
            surface=surface,
        )
        return {
            "object": "list",
            "data": [profile.model_dump(mode="json") for profile in profiles],
        }

    @staticmethod
    def _state_view(operation: OperationView, state: ScientificBatchState) -> dict[str, Any]:
        view = ScientificBatchStatusResponse(
            operation=operation,
            batch=ScientificBatchView(
                batch_id=state.batch_id,
                workload_id=state.workload_id,
                model_id=state.model_id,
                variant_id=state.variant_id,
                input_artifact_id=state.input_artifact_id,
                status=state.status,
                revision=state.revision,
                cancel_requested=state.cancel_requested,
                failure_code=state.failure_code,
                result_published=state.result_published,
                scheduling_snapshot_digest=state.scheduling.digest,
                service_class=state.scheduling.service_class,
                workload_namespace=state.scheduling.workload_namespace,
                route_namespace=state.scheduling.route_namespace,
                stages=tuple(
                    ScientificStageView(
                        stage_id=stage.stage_id,
                        status=stage.status,
                        failure_code=stage.failure_code,
                        attempts=tuple(
                            ScientificAttemptView(
                                attempt_id=attempt.attempt_id,
                                shard_id=attempt.shard_id,
                                attempt_number=attempt.attempt_number,
                                workload_kind=attempt.workload.kind,
                                workload_name=attempt.workload.name,
                                workload_uid=attempt.workload.uid,
                                workload_namespace=attempt.workload.namespace,
                                route_namespace=attempt.workload.route_namespace or attempt.workload.namespace,
                                outcome=attempt.outcome,
                                last_phase=attempt.last_phase,
                                resource_released=attempt.resource_released,
                                scheduling_admission=(
                                    None
                                    if attempt.scheduling_admission is None
                                    or attempt.scheduling_admission.admitted_at is None
                                    else PublicSchedulingAdmission(
                                        resolved_pool_id=attempt.scheduling_admission.resolved_pool_id,
                                        admitted_resource_flavor=attempt.scheduling_admission.admitted_resource_flavor,
                                        accelerator_resource_name=attempt.scheduling_admission.accelerator_resource_name,
                                        accelerator_count=attempt.scheduling_admission.accelerator_count,
                                        admitted_at=attempt.scheduling_admission.admitted_at,
                                    )
                                ),
                                failure_kind=attempt.failure_kind,
                                failure_code=attempt.failure_code,
                            )
                            for attempt in stage.attempts
                        ),
                    )
                    for stage in state.stages
                ),
            ),
        )
        return view.model_dump(mode="json")

    @staticmethod
    def _event_view(event: BatchEvent) -> dict[str, Any]:
        draft = event.draft
        return ScientificEventView(
            sequence=event.sequence,
            event_id=draft.event_id,
            operation_id=draft.operation_id,
            batch_id=draft.batch_id,
            workload_id=draft.workload_id,
            kind=draft.kind,
            stage_id=draft.stage_id,
            shard_id=draft.shard_id,
            attempt_id=draft.attempt_id,
            phase=draft.phase,
            code=draft.code,
            occurred_at=event.occurred_at,
        ).model_dump(mode="json")

    async def submit(
        self,
        *,
        principal: Principal,
        model_id: str,
        request: object,
        idempotency_key: str,
        traceparent: str | None = None,
        require_mcp_invocable: bool = False,
    ) -> dict[str, Any]:
        self._authorize(principal, Scope.INFERENCE_INVOKE, model_id=model_id)
        profile = self.profiles.get(model_id)
        variant_id = self.execution_binding.variant_id(model_id)
        workload_namespace = self.execution_binding.workload_namespace(model_id)
        if require_mcp_invocable and not profile.mcp_invocable:
            raise ScientificProfileError("scientific workload profile is not MCP-invocable")
        validated = self.profiles.validate_request(profile, request)
        input_admission = await self.artifacts.validate_input(
            validated["input_manifest"], tenant_id=principal.tenant_id
        )
        # Input artifacts are caller-owned scientific data, not license
        # credentials. Academic runtime authorization is deployment-bound and
        # projected from the reviewed execution handoff, never supplied by a
        # request or copied from an input-manifest entry.
        try:
            access_context = self.execution_binding.access_context(profile, tenant_id=principal.tenant_id)
        except CatalogProfileAdapterError as error:
            raise ScientificProfileError("scientific workload deployment authorization is not runnable") from error
        try:
            preflight = self.plan_factory.plan(
                profile,
                validated,
                operation_id=UUID(int=0),
                access_context=access_context,
                input_artifacts=input_admission.manifest.entries,
            )
        except CatalogProfileAdapterError as error:
            raise ScientificProfileError("scientific workload profile cannot form an execution plan") from error
        plan = preflight.controller_plan if isinstance(preflight, AdapterExecutionPlan) else preflight
        runtime_artifacts = (
            self.execution_binding.verify_runtime_artifacts(profile, preflight, access_context)
            if isinstance(preflight, AdapterExecutionPlan)
            else ()
        )
        possible_attempts = sum(len(stage.workload_units) * stage.max_attempts for stage in plan.stages)
        if possible_attempts > self.profiles.max_result_attempts:
            raise ScientificProfileError("scientific plan exceeds the canonical public result attempt bound")
        # Resolve every operator-owned value before admitting the Operation.
        # The real snapshot is recaptured below at the durable accepted_at so
        # concurrent idempotent submissions derive byte-identical state.
        try:
            self.scheduling.freeze(
                service_class=validated["service_class"],
                model_id=model_id,
                tenant_id=principal.tenant_id,
                profile=profile.value,
                plan=plan,
                workload_namespace=workload_namespace,
                captured_at=datetime(1970, 1, 1, tzinfo=UTC),
            )
        except SchedulingContractError as error:
            raise ScientificProfileError("Kueue scheduling contract cannot admit this profile") from error
        body = json.dumps(validated, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        def freeze_admission(operation: OperationView) -> dict[str, object]:
            try:
                snapshot = self.scheduling.freeze(
                    service_class=validated["service_class"],
                    model_id=model_id,
                    tenant_id=principal.tenant_id,
                    profile=profile.value,
                    plan=plan,
                    workload_namespace=workload_namespace,
                    captured_at=operation.accepted_at,
                )
            except SchedulingContractError as error:  # defensive against mutable custom resolvers
                raise ScientificProfileError("Kueue scheduling contract changed during admission") from error
            try:
                admitted_execution = self.plan_factory.plan(
                    profile,
                    validated,
                    operation_id=operation.id,
                    access_context=access_context,
                    input_artifacts=input_admission.manifest.entries,
                )
            except CatalogProfileAdapterError as error:
                raise ScientificProfileError("scientific workload profile cannot form an execution plan") from error
            if isinstance(admitted_execution, AdapterExecutionPlan):
                if admitted_execution.controller_plan != plan:
                    raise ScientificProfileError("adapter changed stage topology after durable admission")
                execution_plan = self.execution_binding.bind_runtime_artifacts(
                    profile,
                    admitted_execution,
                    access_context,
                    runtime_artifacts,
                )
            else:
                execution_plan = None
            return state_to_value(
                ScientificBatchState.admit(
                    operation_id=operation.id,
                    tenant_id=principal.tenant_id,
                    model_id=model_id,
                    variant_id=variant_id,
                    input_artifact_id=UUID(validated["input_manifest"]["artifact_id"]),
                    plan=plan,
                    scheduling=snapshot,
                    execution_plan=execution_plan,
                    access_context=access_context,
                    input_manifest=input_admission.manifest,
                    runtime_artifacts=runtime_artifacts,
                )
            )

        operation = await self.store.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id=model_id,
                operation=validated["operation"],
                protocol="scientific-batch-v1",
                idempotency_key=idempotency_key,
                request_body=body,
                request_content_type="application/json",
                traceparent=traceparent,
            ),
            model_revision=profile.model_revision,
            # Stage/resource exact accounting is emitted by the lifecycle
            # ledger; no guessed reservation is charged to the generic worker.
            reserved_gpu_seconds=0,
            max_attempts=1,
            scientific_admission_factory=freeze_admission,
        )
        state = None
        if operation.reused:
            try:
                state = await self.repository.get(operation.id, tenant_id=principal.tenant_id)
            except ScientificBatchNotFoundError:
                # The committed outbox is the recovery authority when an
                # earlier process exited before materializing the extension.
                pass
            else:
                await self.store.complete_scientific_admission(operation.id)
        if state is None:
            try:
                state = await self._materialize_admission(operation.id)
            except BatchRepositoryConflictError as error:
                raise ConflictError("scientific batch admission conflicts with its durable Operation") from error
        return self._state_view(operation, state)

    async def _materialize_pending(self, pending: PendingScientificAdmission) -> ScientificBatchState:
        state = state_from_value(pending.payload)
        operation = await self.store.get_operation(state.operation_id, tenant_id=state.tenant_id)
        if (
            operation.id != pending.operation_id
            or operation.protocol != "scientific-batch-v1"
            or operation.model_id != state.model_id
            or operation.accepted_at != state.scheduling.captured_at
        ):
            raise BatchRepositoryConflictError("scientific admission outbox differs from its durable Operation")
        admitted = await self.controller.admit(
            operation_id=state.operation_id,
            tenant_id=state.tenant_id,
            model_id=state.model_id,
            variant_id=state.variant_id,
            input_artifact_id=state.input_artifact_id,
            plan=state.plan,
            scheduling=state.scheduling,
            execution_plan=state.execution_plan,
            access_context=state.access_context,
            input_manifest=state.input_manifest,
            runtime_artifacts=state.runtime_artifacts,
        )
        await self.store.complete_scientific_admission(state.operation_id)
        return admitted

    async def _materialize_admission(self, operation_id: UUID) -> ScientificBatchState:
        pending = await self.store.get_scientific_admission(operation_id)
        if pending is None:
            operation = await self.store.get_operation(operation_id)
            return await self.repository.get(operation_id, tenant_id=operation.tenant_id)
        return await self._materialize_pending(pending)

    async def recover_pending_admissions(self, *, limit: int = 100) -> int:
        """Materialize accepted batch requests left by a stopped API process."""

        pending = await self.store.list_scientific_admissions(limit=limit)
        for item in pending:
            await self._materialize_pending(item)
        return len(pending)

    async def status(self, operation_id: UUID, *, principal: Principal) -> dict[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_READ)
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != "scientific-batch-v1":
            raise ScientificBatchNotFoundError("operation is not a scientific batch")
        state = await self.repository.get(operation_id, tenant_id=principal.tenant_id)
        return self._state_view(operation, state)

    async def cancel(self, operation_id: UUID, *, principal: Principal) -> dict[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_CANCEL)
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != "scientific-batch-v1":
            raise ScientificBatchNotFoundError("operation is not a scientific batch")
        state = await self.repository.request_cancel(
            operation_id, tenant_id=principal.tenant_id, actor=principal.principal_id
        )
        return self._state_view(operation, state)

    async def events(
        self,
        operation_id: UUID,
        *,
        principal: Principal,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_READ)
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != "scientific-batch-v1":
            raise ScientificBatchNotFoundError("operation is not a scientific batch")
        state = await self.repository.get(operation_id, tenant_id=principal.tenant_id)
        events = await self.repository.list_events(
            operation_id, tenant_id=principal.tenant_id, after_sequence=after_sequence, limit=limit
        )
        return ScientificEventPage(
            operation_id=operation_id,
            batch_id=state.batch_id,
            workload_id=state.workload_id,
            model_id=state.model_id,
            variant_id=state.variant_id,
            data=tuple(ScientificEventView.model_validate(self._event_view(event)) for event in events),
        ).model_dump(mode="json")

    async def artifact(self, artifact_id: UUID, *, principal: Principal) -> Mapping[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_RESULT)
        artifact = ArtifactRef.model_validate(
            await self.artifacts.artifact_response(artifact_id, tenant_id=principal.tenant_id)
        )
        return artifact.model_dump(mode="json", exclude_unset=True)

    async def result(self, operation_id: UUID, *, principal: Principal) -> Mapping[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_RESULT)
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != "scientific-batch-v1":
            raise ScientificBatchNotFoundError("operation is not a scientific batch")
        result = ScientificRunResult.model_validate(
            await self.artifacts.result_response(operation_id, tenant_id=principal.tenant_id)
        )
        document = result.to_document()
        self.profiles.validate_result(document)
        return document
