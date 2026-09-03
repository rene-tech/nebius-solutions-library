"""Integration protocols for the scientific-batch controller."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    AttemptArtifactCommit,
    BatchClaim,
    BatchEventDraft,
    RuntimeArtifactLocalization,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    VerifiedInputManifest,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
)


class BatchRepositoryConflictError(RuntimeError):
    """A compare-and-swap revision or immutable admission value changed."""


class BatchFenceLostError(RuntimeError):
    """The claim no longer authorizes a durable state transition."""


class ScientificBatchRepository(Protocol):
    """Durable extension repository bound to the existing Operation ID.

    Implementations must make ``replace`` and event insertion one transaction,
    reject stale fencing tokens, enforce ``expected_revision``, and deduplicate
    events by ``event_id``. ``create`` must verify that the parent Operation
    exists and freeze the supplied plan and scheduling snapshot on first use.
    """

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
    ) -> ScientificBatchState: ...

    async def claim_next(
        self,
        *,
        controller_id: str,
        lease_seconds: float,
        now: datetime,
    ) -> BatchClaim | None: ...

    async def load(self, claim: BatchClaim) -> ScientificBatchState: ...

    async def replace(
        self,
        claim: BatchClaim,
        *,
        expected_revision: int,
        record: ScientificBatchState,
        events: tuple[BatchEventDraft, ...],
        now: datetime,
    ) -> ScientificBatchState: ...

    async def release(self, claim: BatchClaim) -> None: ...


class ScientificBatchArtifactLifecycle(Protocol):
    """Canonical artifact-service integration; it owns all artifact persistence."""

    async def open_attempt(self, resource: WorkloadResource, *, started_at: datetime) -> None: ...

    async def close_attempt(self, state: ScientificBatchState, attempt: ScientificAttemptState) -> None: ...

    async def ensure_stage_commit(self, state: ScientificBatchState, *, stage_id: str) -> None: ...

    async def artifact_commits(
        self, state: ScientificBatchState, *, stage_id: str
    ) -> tuple[AttemptArtifactCommit, ...]: ...


class LegacyArtifactCommitReader(Protocol):
    """In-memory core-test seam retained without a second production table."""

    async def artifact_commits(self, claim: BatchClaim, *, stage_id: str) -> tuple[AttemptArtifactCommit, ...]: ...


class ScientificBatchCluster(Protocol):
    """Idempotent fenced Kubernetes/Kueue writer and observer.

    ``apply`` must use the deterministic name and attempt identity as immutable
    ownership. Mutations and deletes must reject an older controller fence.
    Implementations render internal `independent-jobs` plans as Jobs and
    `gang-jobset` plans as JobSets; they must not silently substitute one kind
    for the other.
    """

    async def apply(self, resource: WorkloadResource, *, controller_fence: int) -> WorkloadRef: ...

    async def observe(self, ref: WorkloadRef) -> WorkloadObservation: ...

    async def delete(self, ref: WorkloadRef, *, controller_fence: int) -> None: ...

    async def absent(self, ref: WorkloadRef) -> bool: ...


class ScientificBatchResultPublisher(Protocol):
    """Idempotently commit the artifact-service-owned terminal result."""

    async def publish_terminal(self, state: ScientificBatchState) -> None: ...
