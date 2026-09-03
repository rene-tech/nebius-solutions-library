"""Authenticated admin projections over scientific controller and artifact sources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from .admin import AdminProblemError
from .admin_models import (
    AdminContext,
    AdminEnvelope,
    AdminMeta,
    AdminSource,
    AdminSourceState,
    AdminWarning,
)
from .scientific_admin_models import (
    ScientificArtifact,
    ScientificError,
    ScientificModelReadinessList,
    ScientificRunDetail,
    ScientificRunList,
    ScientificSemanticValidation,
    ScientificServiceClass,
)


class ScientificAdminSourceUnavailableError(RuntimeError):
    """A bounded source failure safe to turn into admin availability state."""


class ScientificAdminQueryError(ValueError):
    """A client-supplied scientific admin query cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class ScientificRunQuery:
    from_at: datetime
    to_at: datetime
    limit: int = 100
    cursor: str | None = None
    tenant_id: str | None = None
    model_id: str | None = None
    service_class: ScientificServiceClass | None = None
    access_state: str | None = None
    admission_state: str | None = None
    run_status: str | None = None

    def __post_init__(self) -> None:
        if self.from_at.tzinfo is None or self.to_at.tzinfo is None:
            raise ValueError("scientific run query timestamps must be timezone-aware")
        if self.from_at >= self.to_at or (self.to_at - self.from_at).total_seconds() > 31 * 24 * 60 * 60:
            raise ValueError("scientific run query window is outside the bound")
        if not 1 <= self.limit <= 200:
            raise ValueError("scientific run query limit is outside the bound")


@dataclass(frozen=True, slots=True)
class ScientificRunListSnapshot:
    data: ScientificRunList
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ScientificRunDetailSnapshot:
    data: ScientificRunDetail
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ScientificArtifactAttemptEvidence:
    attempt_id: str
    status: Literal["succeeded", "failed", "preempted", "cancelled"]
    started_at: datetime
    completed_at: datetime
    workload_uid: str | None
    job_uid: str | None
    pod_count: int
    node_count: int
    gpu_count: int | None
    checkpoint_input_artifact_id: str | None
    checkpoint_output_artifact_id: str | None
    admitted_at: datetime | None
    resolved_pool_id: str | None
    admitted_resource_flavor: str | None
    accelerator_resource_name: str | None


@dataclass(frozen=True, slots=True)
class ScientificArtifactSnapshot:
    artifacts: tuple[ScientificArtifact, ...]
    semantic_validation: ScientificSemanticValidation
    observed_at: datetime
    terminal_status: Literal["succeeded", "failed", "cancelled"] | None = None
    completed_at: datetime | None = None
    model_revision: str | None = None
    runtime_image_digest: str | None = None
    execution_identity_digest: str | None = None
    access_profile: Literal["standard", "academic"] | None = None
    access_state: Literal["not-required", "verified"] | None = None
    access_receipt_digest: str | None = None
    service_class: ScientificServiceClass | None = None
    attempts: tuple[ScientificArtifactAttemptEvidence, ...] = ()
    error: ScientificError | None = None


@dataclass(frozen=True, slots=True)
class ScientificModelSnapshot:
    data: ScientificModelReadinessList
    observed_at: datetime


class ScientificRunAdminAdapter(Protocol):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot: ...

    async def get_run(self, operation_id: UUID, *, tenant_id: str | None) -> ScientificRunDetailSnapshot: ...


class ScientificArtifactAdminAdapter(Protocol):
    async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot: ...


class ScientificModelAdminAdapter(Protocol):
    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot: ...


ScientificDataT = TypeVar(
    "ScientificDataT",
    ScientificRunList,
    ScientificRunDetail,
    ScientificModelReadinessList,
)


def _source(
    source_id: str,
    state: AdminSourceState,
    *,
    now: datetime,
    observed_at: datetime | None = None,
    reason: str | None = None,
) -> AdminSource:
    age = max(0.0, (now - observed_at.astimezone(UTC)).total_seconds()) if observed_at is not None else None
    return AdminSource(
        id=source_id,
        state=state,
        observed_at=observed_at,
        age_seconds=age,
        reason=reason,
    )


def _warning(source: AdminSource) -> AdminWarning | None:
    if source.state is AdminSourceState.AVAILABLE:
        return None
    return AdminWarning(
        source=source.id,
        code="partial_source_stale" if source.state is AdminSourceState.STALE else "partial_source_unavailable",
        message=f"{source.id} data is {source.state.value}; affected scientific values are unavailable",
    )


class ScientificAdminReadService:
    """Compose controller, artifact, and catalog reads without hiding source loss."""

    def __init__(
        self,
        *,
        runs: ScientificRunAdminAdapter,
        artifacts: ScientificArtifactAdminAdapter,
        models: ScientificModelAdminAdapter,
        source_max_age_seconds: float = 90,
        adapter_timeout_seconds: float = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= source_max_age_seconds <= 3600:
            raise ValueError("scientific admin source age is outside the bound")
        if not 0.1 <= adapter_timeout_seconds <= 10:
            raise ValueError("scientific admin adapter timeout is outside the bound")
        self.runs = runs
        self.artifacts = artifacts
        self.models = models
        self.source_max_age_seconds = source_max_age_seconds
        self.adapter_timeout_seconds = adapter_timeout_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def _available_source(self, source_id: str, observed_at: datetime, now: datetime) -> AdminSource:
        age = max(0.0, (now - observed_at.astimezone(UTC)).total_seconds())
        if age <= self.source_max_age_seconds:
            return _source(source_id, AdminSourceState.AVAILABLE, now=now, observed_at=observed_at)
        return _source(
            source_id,
            AdminSourceState.STALE,
            now=now,
            observed_at=observed_at,
            reason="scientific observation exceeded the freshness bound",
        )

    @staticmethod
    def _merge_artifact_evidence(
        detail: ScientificRunDetail,
        snapshot: ScientificArtifactSnapshot,
    ) -> ScientificRunDetail:
        run = detail.run
        backend = run.model.backend.model_copy(
            update={
                "model_revision": snapshot.model_revision or run.model.backend.model_revision,
                "runtime_image_digest": snapshot.runtime_image_digest or run.model.backend.runtime_image_digest,
                "execution_identity_digest": (
                    snapshot.execution_identity_digest or run.model.backend.execution_identity_digest
                ),
            }
        )
        access = run.access
        if snapshot.access_profile is not None and snapshot.access_state is not None:
            access = access.model_copy(
                update={
                    "profile": snapshot.access_profile,
                    "state": snapshot.access_state,
                    "receipt_digest": snapshot.access_receipt_digest,
                }
            )
        attempts = {item.attempt_id: item for item in snapshot.attempts}
        stages = []
        for stage in detail.stages:
            stages.append(
                stage.model_copy(
                    update={
                        "attempts": [
                            attempt.model_copy(
                                update={
                                    "status": evidence.status,
                                    "started_at": evidence.started_at,
                                    "completed_at": evidence.completed_at,
                                    "workload_uid": evidence.workload_uid,
                                    "job_uid": evidence.job_uid,
                                    "pod_count": evidence.pod_count,
                                    "node_count": evidence.node_count,
                                    "gpu_count": evidence.gpu_count,
                                    "admitted_at": evidence.admitted_at,
                                    "resolved_pool_id": evidence.resolved_pool_id,
                                    "admitted_resource_flavor": evidence.admitted_resource_flavor,
                                    "accelerator_resource_name": evidence.accelerator_resource_name,
                                    "checkpoint_input_artifact_id": evidence.checkpoint_input_artifact_id,
                                    "checkpoint_output_artifact_id": evidence.checkpoint_output_artifact_id,
                                }
                            )
                            if (evidence := attempts.get(attempt.id)) is not None
                            else attempt
                            for attempt in stage.attempts
                        ]
                    }
                )
            )
        gpu_counts = [item.gpu_count for item in snapshot.attempts if item.gpu_count is not None]
        admitted = [item.admitted_at for item in snapshot.attempts if item.admitted_at is not None]
        terminal_status = snapshot.terminal_status or run.status
        run = run.model_copy(
            update={
                "status": terminal_status,
                "completed_at": snapshot.completed_at or run.completed_at,
                "model": run.model.model_copy(update={"backend": backend}),
                "access": access,
                "service_class": run.service_class.model_copy(
                    update={"effective": snapshot.service_class or run.service_class.effective}
                ),
                "queue": run.queue.model_copy(
                    update={
                        "admission_state": "finished"
                        if snapshot.terminal_status is not None
                        else run.queue.admission_state,
                        "admitted_at": min(admitted) if admitted else run.queue.admitted_at,
                    }
                ),
                "gpu_accounting": run.gpu_accounting.model_copy(
                    update={"gpu_count": max(gpu_counts) if gpu_counts else run.gpu_accounting.gpu_count}
                ),
                "error": (
                    snapshot.error
                    if snapshot.terminal_status == "failed"
                    else None
                    if snapshot.terminal_status is not None
                    else run.error
                ),
            }
        )
        return detail.model_copy(update={"run": run, "stages": stages})

    @staticmethod
    def _envelope(
        context: AdminContext,
        data: ScientificDataT,
        *,
        now: datetime,
        sources: list[AdminSource],
    ) -> AdminEnvelope[ScientificDataT]:
        warnings = [warning for source in sources if (warning := _warning(source)) is not None]
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=sources, warnings=warnings),
            data=data,
        )

    async def run_list(
        self,
        context: AdminContext,
        query: ScientificRunQuery,
    ) -> AdminEnvelope[ScientificRunList]:
        now = self.clock().astimezone(UTC)
        try:
            snapshot = await asyncio.wait_for(
                self.runs.list_runs(query),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except ScientificAdminQueryError:
            raise AdminProblemError(
                422,
                "invalid_scientific_query",
                "the scientific run query is invalid",
            ) from None
        except (OSError, RuntimeError, TimeoutError, ValueError):
            raise AdminProblemError(
                503,
                "scientific_controller_unavailable",
                "scientific controller reporting is unavailable",
            ) from None
        source = self._available_source("scientific-controller", snapshot.observed_at, now)
        return self._envelope(context, snapshot.data, now=now, sources=[source])

    async def run_detail(
        self,
        context: AdminContext,
        operation_id: UUID,
        *,
        tenant_id: str | None,
    ) -> AdminEnvelope[ScientificRunDetail]:
        now = self.clock().astimezone(UTC)
        try:
            run_snapshot = await asyncio.wait_for(
                self.runs.get_run(operation_id, tenant_id=tenant_id),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except KeyError:
            raise AdminProblemError(404, "scientific_run_not_found", "scientific run was not found") from None
        except (OSError, RuntimeError, TimeoutError, ValueError):
            raise AdminProblemError(
                503,
                "scientific_controller_unavailable",
                "scientific controller reporting is unavailable",
            ) from None

        sources = [self._available_source("scientific-controller", run_snapshot.observed_at, now)]
        detail = run_snapshot.data
        try:
            artifact_snapshot = await asyncio.wait_for(
                self.artifacts.for_operation(operation_id, tenant_id=detail.run.attribution.tenant_id),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
            sources.append(
                _source(
                    "scientific-artifacts",
                    AdminSourceState.UNAVAILABLE,
                    now=now,
                    reason="scientific artifact reporting is unavailable",
                )
            )
            detail = detail.model_copy(
                update={
                    "artifacts": [],
                    "semantic_validation": ScientificSemanticValidation(
                        validator_id="unavailable",
                        status="not-run",
                        receipt_digest=None,
                    ),
                },
            )
        else:
            sources.append(self._available_source("scientific-artifacts", artifact_snapshot.observed_at, now))
            detail = self._merge_artifact_evidence(detail, artifact_snapshot).model_copy(
                update={
                    "artifacts": list(artifact_snapshot.artifacts),
                    "semantic_validation": artifact_snapshot.semantic_validation,
                },
            )
        return self._envelope(context, detail, now=now, sources=sources)

    async def model_list(
        self,
        context: AdminContext,
        *,
        tenant_id: str | None = None,
    ) -> AdminEnvelope[ScientificModelReadinessList]:
        now = self.clock().astimezone(UTC)
        try:
            snapshot = await asyncio.wait_for(
                self.models.list_models(tenant_id=tenant_id),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError):
            raise AdminProblemError(
                503,
                "scientific_catalog_unavailable",
                "scientific model readiness is unavailable",
            ) from None
        source = self._available_source("scientific-catalog", snapshot.observed_at, now)
        return self._envelope(context, snapshot.data, now=now, sources=[source])
