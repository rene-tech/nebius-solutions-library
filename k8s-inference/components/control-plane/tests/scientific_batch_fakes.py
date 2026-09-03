"""Fenced deterministic fakes for scientific-batch controller tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from fs2_serve.scientific_batch.models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    AttemptArtifactCommit,
    BatchClaim,
    BatchEvent,
    BatchEventDraft,
    RuntimeArtifactLocalization,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
    StageSchedulingDecision,
    VerifiedInputManifest,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
)
from fs2_serve.scientific_batch.postgres_repository import ScientificBatchNotFoundError
from fs2_serve.scientific_batch.protocols import BatchFenceLostError, BatchRepositoryConflictError


class FakeScientificBatchRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, ScientificBatchState] = {}
        self.events: dict[UUID, list[BatchEvent]] = {}
        self.commits: dict[tuple[UUID, str, UUID], AttemptArtifactCommit] = {}
        self._claims: dict[UUID, BatchClaim] = {}
        self._fences: dict[UUID, int] = {}
        self.fail_next_replace = False

    async def create(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        model_id: str,
        variant_id: str,
        input_artifact_id: UUID,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        execution_plan: AdapterExecutionPlan | None = None,
        access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT,
        input_manifest: VerifiedInputManifest | None = None,
        runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = (),
    ) -> ScientificBatchState:
        proposed = ScientificBatchState.admit(
            operation_id=operation_id,
            tenant_id=tenant_id,
            model_id=model_id,
            variant_id=variant_id,
            input_artifact_id=input_artifact_id,
            plan=plan,
            scheduling=scheduling,
            execution_plan=execution_plan,
            access_context=access_context,
            input_manifest=input_manifest,
            runtime_artifacts=runtime_artifacts,
        )
        current = self.records.get(operation_id)
        if current is None:
            self.records[operation_id] = proposed
            self.events[operation_id] = []
            return proposed
        if (
            current.tenant_id != tenant_id
            or current.model_id != model_id
            or current.variant_id != variant_id
            or current.input_artifact_id != input_artifact_id
            or current.plan != plan
            or current.scheduling != scheduling
            or current.execution_plan != execution_plan
            or current.access_context != access_context
            or current.input_manifest != input_manifest
            or current.runtime_artifacts != runtime_artifacts
        ):
            raise BatchRepositoryConflictError("operation already has a different frozen batch admission")
        return current

    async def claim_next(self, *, controller_id: str, lease_seconds: float, now) -> BatchClaim | None:
        for operation_id, record in self.records.items():
            if (record.status.terminal and record.result_published) or operation_id in self._claims:
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
        if self.fail_next_replace:
            self.fail_next_replace = False
            raise RuntimeError("injected durable replace failure")
        current = self.records[claim.operation_id]
        if current.revision != expected_revision or record.revision != expected_revision + 1:
            raise BatchRepositoryConflictError("scientific-batch revision changed")
        if current.cancel_requested and not record.cancel_requested:
            record = replace(record, cancel_requested=True)
        if (
            record.operation_id != current.operation_id
            or record.batch_id != current.batch_id
            or record.workload_id != current.workload_id
            or record.tenant_id != current.tenant_id
            or record.model_id != current.model_id
            or record.variant_id != current.variant_id
            or record.input_artifact_id != current.input_artifact_id
            or record.plan != current.plan
            or record.scheduling != current.scheduling
            or record.execution_plan != current.execution_plan
            or record.access_context != current.access_context
            or record.input_manifest != current.input_manifest
            or record.runtime_artifacts != current.runtime_artifacts
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

    async def artifact_commits(self, claim: BatchClaim, *, stage_id: str) -> tuple[AttemptArtifactCommit, ...]:
        self._assert_claim(claim)
        return tuple(
            value
            for (operation_id, stored_stage, _), value in self.commits.items()
            if operation_id == claim.operation_id and stored_stage == stage_id
        )

    async def list_artifact_commits(self, operation_id: UUID, *, tenant_id: str) -> tuple[AttemptArtifactCommit, ...]:
        await self.get(operation_id, tenant_id=tenant_id)
        return tuple(
            value for (stored_operation, _, _), value in self.commits.items() if stored_operation == operation_id
        )

    async def record_artifact_commit(
        self,
        commit: AttemptArtifactCommit,
        *,
        tenant_id: str,
        manifest_artifact_id: UUID,
        validation_artifact_id: UUID,
    ) -> AttemptArtifactCommit:
        if (manifest_artifact_id, validation_artifact_id) != (
            commit.manifest_artifact_id,
            commit.validation_artifact_id,
        ):
            raise BatchRepositoryConflictError("stage commit artifact IDs differ")
        record = self.records.get(commit.operation_id)
        if record is None or record.tenant_id != tenant_id:
            raise ScientificBatchNotFoundError("scientific batch does not exist")
        key = (commit.operation_id, commit.stage_id, commit.attempt_ids[0])
        current = self.commits.get(key)
        if current is not None and current != commit:
            raise BatchRepositoryConflictError("stage already has another artifact commit")
        self.commits[key] = commit
        return commit

    async def release(self, claim: BatchClaim) -> None:
        if self._claims.get(claim.operation_id) == claim:
            del self._claims[claim.operation_id]

    async def assert_fence(self, operation_id: UUID, *, controller_id: str, fencing_token: int) -> None:
        claim = self._claims.get(operation_id)
        if claim is None or claim.controller_id != controller_id or claim.fencing_token != fencing_token:
            raise BatchFenceLostError("stale scientific-batch claim")

    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState:
        current = self.records.get(operation_id)
        if current is None or current.tenant_id != tenant_id:
            raise ScientificBatchNotFoundError("scientific batch does not exist")
        return current

    async def request_cancel(self, operation_id: UUID, *, tenant_id: str, actor: str) -> ScientificBatchState:
        del actor
        current = await self.get(operation_id, tenant_id=tenant_id)
        if not current.status.terminal and not current.cancel_requested:
            current = replace(current, cancel_requested=True)
            self.records[operation_id] = current
        return current

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_sequence: int = 0, limit: int = 1000
    ) -> list[BatchEvent]:
        await self.get(operation_id, tenant_id=tenant_id)
        return [event for event in self.events[operation_id] if event.sequence > after_sequence][:limit]

    def force_cancel(self, operation_id: UUID) -> None:
        current = self.records[operation_id]
        self.records[operation_id] = replace(current, cancel_requested=True, revision=current.revision + 1)

    def put_commit(self, commit: AttemptArtifactCommit) -> None:
        self.commits[(commit.operation_id, commit.stage_id, commit.attempt_ids[0])] = commit


class FakeScientificBatchCluster:
    def __init__(self) -> None:
        self.resources: dict[tuple[str, str, str], WorkloadResource] = {}
        self.refs: dict[tuple[str, str, str], WorkloadRef] = {}
        self.observations: dict[tuple[str, str, str], WorkloadObservation] = {}
        self.fences: dict[tuple[str, str, str], int] = {}
        self.apply_history: list[WorkloadResource] = []
        self.delete_history: list[WorkloadRef] = []
        self.delete_calls: list[WorkloadRef] = []
        self.deletion_polls_before_absent: dict[tuple[str, str, str], int] = {}
        self.absence_polls: list[WorkloadRef] = []

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

    async def observe(
        self,
        ref: WorkloadRef,
        *,
        scheduling: StageSchedulingDecision,
    ) -> WorkloadObservation:
        del scheduling
        return self.observations[self.key(ref)]

    async def delete(self, ref: WorkloadRef, *, controller_fence: int) -> None:
        key = self.key(ref)
        if controller_fence < self.fences.get(key, 0):
            raise BatchFenceLostError("stale cluster delete fence")
        self.fences[key] = controller_fence
        self.delete_calls.append(ref)
        if ref not in self.delete_history:
            self.delete_history.append(ref)

    async def absent(self, ref: WorkloadRef) -> bool:
        key = self.key(ref)
        self.absence_polls.append(ref)
        current = self.refs.get(key)
        if current is not None and current.uid != ref.uid:
            raise BatchRepositoryConflictError("workload UID changed while deletion was pending")
        remaining = self.deletion_polls_before_absent.get(key, 0)
        if remaining > 0:
            self.deletion_polls_before_absent[key] = remaining - 1
            return False
        self.resources.pop(key, None)
        self.refs.pop(key, None)
        self.observations.pop(key, None)
        return True

    def set_observation(self, ref: WorkloadRef, observation: WorkloadObservation) -> None:
        self.observations[self.key(ref)] = observation
