"""Authorized API/MCP consumer over durable Operations and controller state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from ..auth import require_operation_access
from ..models import AdmissionRequest, OperationView, Principal, Scope
from ..store import ConflictError, Store
from .catalog_adapter import CatalogProfileAdapterError, scientific_plan_from_catalog_profile
from .controller import ScientificBatchController
from .models import BatchEvent, ScientificBatchPlan, ScientificBatchState
from .postgres_repository import ScientificBatchNotFoundError
from .profile_catalog import ScientificProfileCatalog, ScientificProfileError, ScientificWorkloadProfile
from .protocols import BatchRepositoryConflictError
from .scheduling import SchedulingContractError, SchedulingContractResolver


class ScientificArtifactAccess(Protocol):
    """Consumer seam implemented by the artifact-service owner."""

    async def validate_input(self, pointer: Mapping[str, Any], *, tenant_id: str) -> None: ...

    async def artifact_response(self, artifact_id: UUID, *, tenant_id: str) -> Mapping[str, Any]: ...

    async def result_response(self, operation_id: UUID, *, tenant_id: str) -> Mapping[str, Any]: ...


class ScientificPlanFactory(Protocol):
    def plan(self, profile: ScientificWorkloadProfile, request: Mapping[str, Any]) -> ScientificBatchPlan: ...


class CatalogScientificPlanFactory:
    """Default adapter for profiles whose catalog minimum expands to one unit."""

    def plan(self, profile: ScientificWorkloadProfile, request: Mapping[str, Any]) -> ScientificBatchPlan:
        del request
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
        plan_factory: ScientificPlanFactory | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.controller = controller
        self.profiles = profiles
        self.scheduling = scheduling
        self.artifacts = artifacts
        self.plan_factory = plan_factory or CatalogScientificPlanFactory()

    @staticmethod
    def _authorize(principal: Principal, scope: Scope, *, model_id: str | None = None) -> None:
        principal.require(scope, model_id=model_id)

    @staticmethod
    def _state_view(operation: OperationView, state: ScientificBatchState) -> dict[str, Any]:
        return {
            "operation": operation.model_dump(mode="json"),
            "batch": {
                "batch_id": str(state.batch_id),
                "workload_id": str(state.workload_id),
                "model_id": state.model_id,
                "status": str(state.status),
                "revision": state.revision,
                "cancel_requested": state.cancel_requested,
                "failure_code": state.failure_code,
                "scheduling_snapshot_digest": state.scheduling.digest,
                "service_class": str(state.scheduling.service_class),
                "stages": [
                    {
                        "stage_id": stage.stage_id,
                        "status": str(stage.status),
                        "failure_code": stage.failure_code,
                        "attempts": [
                            {
                                "attempt_id": str(attempt.attempt_id),
                                "shard_id": attempt.shard_id,
                                "attempt_number": attempt.attempt_number,
                                "workload_kind": str(attempt.workload.kind),
                                "workload_name": attempt.workload.name,
                                "workload_uid": attempt.workload.uid,
                                "outcome": str(attempt.outcome),
                                "last_phase": str(attempt.last_phase),
                                "resource_released": attempt.resource_released,
                                "failure_kind": None if attempt.failure_kind is None else str(attempt.failure_kind),
                                "failure_code": attempt.failure_code,
                            }
                            for attempt in stage.attempts
                        ],
                    }
                    for stage in state.stages
                ],
            },
        }

    @staticmethod
    def _event_view(event: BatchEvent) -> dict[str, Any]:
        draft = event.draft
        return {
            "sequence": event.sequence,
            "event_id": draft.event_id,
            "operation_id": str(draft.operation_id),
            "batch_id": str(draft.batch_id),
            "workload_id": str(draft.workload_id),
            "kind": str(draft.kind),
            "stage_id": draft.stage_id,
            "shard_id": draft.shard_id,
            "attempt_id": None if draft.attempt_id is None else str(draft.attempt_id),
            "phase": None if draft.phase is None else str(draft.phase),
            "code": draft.code,
            "occurred_at": event.occurred_at.isoformat(),
        }

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
        if require_mcp_invocable and not profile.mcp_invocable:
            raise ScientificProfileError("scientific workload profile is not MCP-invocable")
        validated = self.profiles.validate_request(profile, request)
        await self.artifacts.validate_input(validated["input_manifest"], tenant_id=principal.tenant_id)
        try:
            plan = self.plan_factory.plan(profile, validated)
        except CatalogProfileAdapterError as error:
            raise ScientificProfileError("scientific workload profile cannot form an execution plan") from error
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
                profile=profile.value,
                plan=plan,
                captured_at=datetime(1970, 1, 1, tzinfo=UTC),
            )
        except SchedulingContractError as error:
            raise ScientificProfileError("Kueue scheduling contract cannot admit this profile") from error
        body = json.dumps(validated, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
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
        )
        try:
            snapshot = self.scheduling.freeze(
                service_class=validated["service_class"],
                model_id=model_id,
                profile=profile.value,
                plan=plan,
                captured_at=operation.accepted_at,
            )
        except SchedulingContractError as error:  # defensive against mutable custom resolvers
            raise ScientificProfileError("Kueue scheduling contract changed during admission") from error
        state = None
        if operation.reused:
            try:
                state = await self.repository.get(operation.id, tenant_id=principal.tenant_id)
            except ScientificBatchNotFoundError:
                # Recovery from a crash between durable Operation admission
                # and extension creation. The first successful create freezes
                # the scheduling snapshot; later racers compare it exactly.
                pass
        if state is None:
            try:
                state = await self.controller.admit(
                    operation_id=operation.id,
                    tenant_id=principal.tenant_id,
                    model_id=model_id,
                    plan=plan,
                    scheduling=snapshot,
                )
            except BatchRepositoryConflictError as error:
                raise ConflictError("scientific batch admission conflicts with its durable Operation") from error
        return self._state_view(operation, state)

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
        events = await self.repository.list_events(
            operation_id, tenant_id=principal.tenant_id, after_sequence=after_sequence, limit=limit
        )
        return {"operation_id": str(operation_id), "data": [self._event_view(event) for event in events]}

    async def artifact(self, artifact_id: UUID, *, principal: Principal) -> Mapping[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_RESULT)
        return await self.artifacts.artifact_response(artifact_id, tenant_id=principal.tenant_id)

    async def result(self, operation_id: UUID, *, principal: Principal) -> Mapping[str, Any]:
        self._authorize(principal, Scope.OPERATIONS_RESULT)
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != "scientific-batch-v1":
            raise ScientificBatchNotFoundError("operation is not a scientific batch")
        result = await self.artifacts.result_response(operation_id, tenant_id=principal.tenant_id)
        self.profiles.validate_result(result)
        return result
