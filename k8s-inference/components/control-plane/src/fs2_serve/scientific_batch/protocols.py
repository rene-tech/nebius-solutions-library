"""Integration protocols for the scientific-batch controller."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import (
    ArtifactCommit,
    BatchClaim,
    BatchEventDraft,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
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
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
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

    async def artifact_commit(self, claim: BatchClaim, *, stage_id: str) -> ArtifactCommit | None: ...

    async def release(self, claim: BatchClaim) -> None: ...


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
