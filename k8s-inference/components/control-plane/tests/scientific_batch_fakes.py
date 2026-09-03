"""Fenced deterministic fakes for scientific-batch controller tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactCommit,
    BatchClaim,
    BatchEvent,
    BatchEventDraft,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
)
from fs2_serve.scientific_batch.protocols import BatchFenceLostError, BatchRepositoryConflictError


class FakeScientificBatchRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, ScientificBatchState] = {}
        self.events: dict[UUID, list[BatchEvent]] = {}
        self.commits: dict[tuple[UUID, str], ArtifactCommit] = {}
        self._claims: dict[UUID, BatchClaim] = {}
        self._fences: dict[UUID, int] = {}

    async def create(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        execution_plan: AdapterExecutionPlan | None = None,
    ) -> ScientificBatchState:
        proposed = ScientificBatchState.admit(
            operation_id=operation_id,
            tenant_id=tenant_id,
            plan=plan,
            scheduling=scheduling,
            execution_plan=execution_plan,
        )
        current = self.records.get(operation_id)
        if current is None:
            self.records[operation_id] = proposed
            self.events[operation_id] = []
            return proposed
        if (
            current.tenant_id != tenant_id
            or current.plan != plan
            or current.scheduling != scheduling
            or current.execution_plan != execution_plan
        ):
            raise BatchRepositoryConflictError("operation already has a different frozen batch admission")
        return current

    async def claim_next(self, *, controller_id: str, lease_seconds: float, now) -> BatchClaim | None:
        for operation_id, record in self.records.items():
            if record.status.terminal or operation_id in self._claims:
                continue
            fence = self._fences.get(operation_id, 0) + 1
            self._fences[operation_id] = fence
            claim = BatchClaim(
                operation_id=operation_id,
                controller_id=controller_id,
                fencing_token=fence,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._claims[operation_id] = claim
            return claim
        return None

    def _assert_claim(self, claim: BatchClaim) -> None:
        if self._claims.get(claim.operation_id) != claim:
            raise BatchFenceLostError("stale scientific-batch claim")

    async def load(self, claim: BatchClaim) -> ScientificBatchState:
        self._assert_claim(claim)
        return self.records[claim.operation_id]

    async def replace(
        self,
        claim: BatchClaim,
        *,
        expected_revision: int,
        record: ScientificBatchState,
        events: tuple[BatchEventDraft, ...],
        now,
    ) -> ScientificBatchState:
        self._assert_claim(claim)
        current = self.records[claim.operation_id]
        if current.revision != expected_revision or record.revision != expected_revision + 1:
            raise BatchRepositoryConflictError("scientific-batch revision changed")
        if (
            record.operation_id != current.operation_id
            or record.batch_id != current.batch_id
            or record.workload_id != current.workload_id
            or record.tenant_id != current.tenant_id
            or record.plan != current.plan
            or record.scheduling != current.scheduling
            or record.execution_plan != current.execution_plan
            or record.model_id != current.model_id
            or record.variant_id != current.variant_id
        ):
            raise BatchRepositoryConflictError("immutable scientific-batch admission changed")
        ledger = self.events[claim.operation_id]
        seen = {event.draft.event_id for event in ledger}
        for draft in events:
            if draft.event_id in seen:
                continue
            ledger.append(BatchEvent(sequence=len(ledger) + 1, occurred_at=now, draft=draft))
            seen.add(draft.event_id)
        self.records[claim.operation_id] = record
        return record

    async def artifact_commit(self, claim: BatchClaim, *, stage_id: str) -> ArtifactCommit | None:
        self._assert_claim(claim)
        return self.commits.get((claim.operation_id, stage_id))

    async def release(self, claim: BatchClaim) -> None:
        if self._claims.get(claim.operation_id) == claim:
            del self._claims[claim.operation_id]

    def request_cancel(self, operation_id: UUID) -> None:
        current = self.records[operation_id]
        self.records[operation_id] = replace(current, cancel_requested=True, revision=current.revision + 1)

    def put_commit(self, commit: ArtifactCommit) -> None:
        self.commits[(commit.operation_id, commit.stage_id)] = commit


class FakeScientificBatchCluster:
    def __init__(self) -> None:
        self.resources: dict[tuple[str, str, str], WorkloadResource] = {}
        self.refs: dict[tuple[str, str, str], WorkloadRef] = {}
        self.observations: dict[tuple[str, str, str], WorkloadObservation] = {}
        self.fences: dict[tuple[str, str, str], int] = {}
        self.apply_history: list[WorkloadResource] = []
        self.delete_history: list[WorkloadRef] = []

    @staticmethod
    def key(ref: WorkloadRef) -> tuple[str, str, str]:
        return (ref.namespace, ref.name, str(ref.kind))

    async def apply(self, resource: WorkloadResource, *, controller_fence: int) -> WorkloadRef:
        key = (resource.namespace, resource.name, str(resource.kind))
        prior_fence = self.fences.get(key, 0)
        if controller_fence < prior_fence:
            raise BatchFenceLostError("stale cluster apply fence")
        existing = self.resources.get(key)
        if existing is not None and existing != resource:
            raise BatchRepositoryConflictError("deterministic workload name has different immutable ownership")
        uid = str(uuid5(NAMESPACE_URL, f"fake-kubernetes:{key}"))
        ref = WorkloadRef(namespace=resource.namespace, name=resource.name, kind=resource.kind, uid=uid)
        self.resources[key] = resource
        self.refs[key] = ref
        self.fences[key] = controller_fence
        self.apply_history.append(resource)
        self.observations.setdefault(
            key,
            WorkloadObservation(
                ref=ref,
                attempt_id=resource.attempt_id,
                state=WorkloadState.PENDING,
                phases=(),
            ),
        )
        return ref

    async def observe(self, ref: WorkloadRef) -> WorkloadObservation:
        return self.observations[self.key(ref)]

    async def delete(self, ref: WorkloadRef, *, controller_fence: int) -> None:
        key = self.key(ref)
        if controller_fence < self.fences.get(key, 0):
            raise BatchFenceLostError("stale cluster delete fence")
        self.fences[key] = controller_fence
        if ref not in self.delete_history:
            self.delete_history.append(ref)

    def set_observation(self, ref: WorkloadRef, observation: WorkloadObservation) -> None:
        self.observations[self.key(ref)] = observation
