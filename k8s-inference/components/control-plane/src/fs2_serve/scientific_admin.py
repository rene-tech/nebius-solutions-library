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
    ScientificCapabilities,
    ScientificCapability,
    ScientificError,
    ScientificModelPolicy,
    ScientificModelPolicyList,
    ScientificModelPolicyUpdate,
    ScientificModelReadinessList,
    ScientificRunDetail,
    ScientificRunList,
    ScientificSemanticValidation,
    ScientificServiceClass,
)


class ScientificAdminSourceUnavailableError(RuntimeError):
    """A bounded source failure safe to turn into admin availability state."""


SCIENTIFIC_RUNS_UNCONFIGURED = "no durable scientific batch controller reader is bound to this build"
SCIENTIFIC_ARTIFACTS_UNCONFIGURED = "no scientific artifact result reader is bound to this build"
SCIENTIFIC_RUN_CONTROL_UNCONFIGURED = "no scientific batch cancellation writer is bound to this build"
SCIENTIFIC_MODEL_POLICY_UNCONFIGURED = "no durable scientific model policy repository is bound to this build"

ScientificRunCancelOutcome = Literal["requested", "already-requested", "terminal"]


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


@dataclass(frozen=True, slots=True)
class ScientificModelPolicySnapshot:
    data: ScientificModelPolicyList
    observed_at: datetime


class ScientificModelPolicyInvalidError(ValueError):
    """A requested model startup option is not available in this deployment."""


class ScientificModelPolicyStaleRevisionError(RuntimeError):
    """The operator's expected policy revision is no longer the durable one."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("scientific model policy revision is stale")
        self.current_revision = current_revision


class ScientificRunAdminAdapter(Protocol):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot: ...

    async def get_run(self, operation_id: UUID, *, tenant_id: str | None) -> ScientificRunDetailSnapshot: ...


class ScientificArtifactAdminAdapter(Protocol):
    async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot: ...


class ScientificModelAdminAdapter(Protocol):
    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot: ...


class ScientificModelPolicyAdminAdapter(Protocol):
    """Durable per-model dispatch policy: pause new dispatch or cap active runs.

    ``list_policies`` returns one item per requested model (plus any model that
    still owns a row or live batch) in the given scope; ``set_policy`` replaces
    one scope row at exactly ``expected_revision`` and raises
    ``ScientificModelPolicyStaleRevisionError`` otherwise. Both read counts and
    the effective decision from the controller's own durable tables.
    """

    async def list_policies(
        self,
        *,
        tenant_id: str | None,
        model_ids: tuple[str, ...],
    ) -> ScientificModelPolicySnapshot: ...

    async def set_policy(
        self,
        model_id: str,
        *,
        tenant_id: str | None,
        update: ScientificModelPolicyUpdate,
        actor: str,
    ) -> ScientificModelPolicy: ...


class ScientificRunControlAdapter(Protocol):
    """The only scientific run command the admin surface issues: a cancel request.

    Implementations record the request durably under the run's own tenant and
    report whether it was newly requested, already pending, or too late because
    the batch had already reached a terminal status. A missing run raises
    ``KeyError`` so the service can answer 404 without leaking backend detail.
    """

    async def request_cancel(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        actor: str,
    ) -> ScientificRunCancelOutcome: ...


ScientificDataT = TypeVar(
    "ScientificDataT",
    ScientificRunList,
    ScientificRunDetail,
    ScientificModelReadinessList,
    ScientificModelPolicyList,
    ScientificModelPolicy,
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
        models: ScientificModelAdminAdapter | None = None,
        runs: ScientificRunAdminAdapter | None = None,
        artifacts: ScientificArtifactAdminAdapter | None = None,
        controls: ScientificRunControlAdapter | None = None,
        policies: ScientificModelPolicyAdminAdapter | None = None,
        source_max_age_seconds: float = 90,
        adapter_timeout_seconds: float = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= source_max_age_seconds <= 3600:
            raise ValueError("scientific admin source age is outside the bound")
        if not 0.1 <= adapter_timeout_seconds <= 10:
            raise ValueError("scientific admin adapter timeout is outside the bound")
        if artifacts is not None and runs is None:
            raise ValueError("scientific artifact reporting requires a run reader")
        if controls is not None and runs is None:
            raise ValueError("scientific run cancellation requires a run reader")
        if policies is not None and models is None:
            raise ValueError("scientific model policy requires the catalog reader")
        self.runs = runs
        self.artifacts = artifacts
        self.controls = controls
        self.policies = policies
        self.models = models
        self.source_max_age_seconds = source_max_age_seconds
        self.adapter_timeout_seconds = adapter_timeout_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def _require_runs(self) -> ScientificRunAdminAdapter:
        if self.runs is None:
            raise AdminProblemError(
                503,
                "scientific_runs_unavailable",
                "scientific run reporting is not configured",
            )
        return self.runs

    def capabilities(self, *, model_readiness_allowed: bool = True) -> ScientificCapabilities:
        """Report which scientific surfaces this build can actually serve.

        The console gates its navigation on this, so an operator is never sent
        to a page whose producer does not exist.
        """

        model_available = self.models is not None and model_readiness_allowed
        if self.models is None:
            model_reason = "the scientific catalog reader is not configured"
        elif not model_readiness_allowed:
            model_reason = "scientific model readiness requires a global operator"
        else:
            model_reason = None
        return ScientificCapabilities(
            model_readiness=ScientificCapability(
                available=model_available,
                reason=model_reason,
            ),
            run_history=ScientificCapability(
                available=self.runs is not None,
                reason=None if self.runs is not None else SCIENTIFIC_RUNS_UNCONFIGURED,
            ),
            artifacts=ScientificCapability(
                available=self.artifacts is not None,
                reason=None if self.artifacts is not None else SCIENTIFIC_ARTIFACTS_UNCONFIGURED,
            ),
            run_control=ScientificCapability(
                available=self.controls is not None,
                reason=None if self.controls is not None else SCIENTIFIC_RUN_CONTROL_UNCONFIGURED,
            ),
            model_policy=ScientificCapability(
                available=self.policies is not None,
                reason=None if self.policies is not None else SCIENTIFIC_MODEL_POLICY_UNCONFIGURED,
            ),
        )

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
        runs = self._require_runs()
        try:
            snapshot = await asyncio.wait_for(
                runs.list_runs(query),
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
        runs = self._require_runs()
        try:
            run_snapshot = await asyncio.wait_for(
                runs.get_run(operation_id, tenant_id=tenant_id),
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
        artifacts = self.artifacts
        if artifacts is None:
            sources.append(
                _source(
                    "scientific-artifacts",
                    AdminSourceState.UNAVAILABLE,
                    now=now,
                    reason=SCIENTIFIC_ARTIFACTS_UNCONFIGURED,
                )
            )
            return self._envelope(context, detail, now=now, sources=sources)
        try:
            artifact_snapshot = await asyncio.wait_for(
                artifacts.for_operation(operation_id, tenant_id=detail.run.attribution.tenant_id),
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

    async def cancel_run(
        self,
        context: AdminContext,
        operation_id: UUID,
        *,
        tenant_id: str | None,
        actor: str,
    ) -> AdminEnvelope[ScientificRunDetail]:
        """Request cancellation of one run and return its refreshed projection.

        The run is first resolved under the operator's own tenant authority, so
        a foreign identifier answers 404 exactly like the read route does. The
        durable request is then recorded under the run's tenant with the
        operator subject as the audited actor; the controller performs the
        actual termination on its next reconciliation.
        """

        controls = self.controls
        if controls is None:
            raise AdminProblemError(
                503,
                "scientific_run_control_unavailable",
                "scientific run cancellation is not configured",
            )
        runs = self._require_runs()
        try:
            snapshot = await asyncio.wait_for(
                runs.get_run(operation_id, tenant_id=tenant_id),
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
        try:
            outcome = await asyncio.wait_for(
                controls.request_cancel(
                    operation_id,
                    tenant_id=snapshot.data.run.attribution.tenant_id,
                    actor=actor,
                ),
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
                "scientific run cancellation is unavailable",
            ) from None
        if outcome == "terminal":
            raise AdminProblemError(
                409,
                "scientific_run_terminal",
                "scientific run already reached a terminal status",
            )
        return await self.run_detail(context, operation_id, tenant_id=tenant_id)

    async def model_list(
        self,
        context: AdminContext,
        *,
        tenant_id: str | None = None,
    ) -> AdminEnvelope[ScientificModelReadinessList]:
        now = self.clock().astimezone(UTC)
        models = self.models
        if models is None:
            raise AdminProblemError(
                503,
                "scientific_catalog_unavailable",
                "scientific model readiness is not configured",
            )
        try:
            snapshot = await asyncio.wait_for(
                models.list_models(tenant_id=tenant_id),
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

    def _require_policies(self) -> ScientificModelPolicyAdminAdapter:
        if self.policies is None:
            raise AdminProblemError(
                503,
                "scientific_model_policy_unavailable",
                "scientific model policy is not configured",
            )
        return self.policies

    async def _known_model_ids(self) -> tuple[str, ...]:
        """Global catalog identities a policy may name; unknown ids answer 404."""

        models = self.models
        if models is None:
            raise AdminProblemError(
                503,
                "scientific_catalog_unavailable",
                "scientific model readiness is not configured",
            )
        try:
            snapshot = await asyncio.wait_for(
                models.list_models(tenant_id=None),
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
        return tuple(sorted({item.model_id for item in snapshot.data.items}))

    async def policy_list(
        self,
        context: AdminContext,
        *,
        tenant_id: str | None = None,
    ) -> AdminEnvelope[ScientificModelPolicyList]:
        now = self.clock().astimezone(UTC)
        policies = self._require_policies()
        model_ids = await self._known_model_ids()
        try:
            snapshot = await asyncio.wait_for(
                policies.list_policies(tenant_id=tenant_id, model_ids=model_ids),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError):
            raise AdminProblemError(
                503,
                "scientific_controller_unavailable",
                "scientific model policy reporting is unavailable",
            ) from None
        source = self._available_source("scientific-controller", snapshot.observed_at, now)
        return self._envelope(context, snapshot.data, now=now, sources=[source])

    async def set_policy(
        self,
        context: AdminContext,
        model_id: str,
        *,
        tenant_id: str | None,
        update: ScientificModelPolicyUpdate,
        actor: str,
    ) -> AdminEnvelope[ScientificModelPolicy]:
        """Replace one model's dispatch policy in the operator's authorized scope.

        The model must be a catalog identity so a typo cannot create a dangling
        row. A stale ``expected_revision`` answers 409 with the durable revision
        so the console can reload rather than overwrite another operator.
        """

        now = self.clock().astimezone(UTC)
        policies = self._require_policies()
        if model_id not in await self._known_model_ids():
            raise AdminProblemError(404, "scientific_model_not_found", "scientific model was not found")
        try:
            policy = await asyncio.wait_for(
                policies.set_policy(model_id, tenant_id=tenant_id, update=update, actor=actor),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except ScientificModelPolicyStaleRevisionError as error:
            raise AdminProblemError(
                409,
                "scientific_model_policy_stale",
                f"scientific model policy revision changed; current revision is {error.current_revision}",
            ) from None
        except ScientificModelPolicyInvalidError as error:
            raise AdminProblemError(422, "scientific_model_policy_invalid", str(error)) from None
        except (OSError, RuntimeError, TimeoutError, ValueError):
            raise AdminProblemError(
                503,
                "scientific_controller_unavailable",
                "scientific model policy update is unavailable",
            ) from None
        source = self._available_source("scientific-controller", now, now)
        return self._envelope(context, policy, now=now, sources=[source])
