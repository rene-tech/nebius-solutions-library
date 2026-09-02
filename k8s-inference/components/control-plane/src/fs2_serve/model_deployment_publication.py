"""Fail-closed dynamic model publication and integration projections.

This module deliberately contains no Kubernetes, PostgreSQL, registry, MCP, or
Prometheus writer.  It turns a current desired revision plus controller-observed
status into a deterministic publication candidate and provides bounded adapter
contracts for queue, autoscaling, cache, and snapshot observations.  A later
atomic publisher may consume the snapshot only after its own fencing checks.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .model_deployment import SHA256_DIGEST_PATTERN, DesiredState, Visibility, canonical_digest
from .model_deployment_records import (
    KubernetesConditionStatus,
    ModelDeploymentConditionType,
    ModelDeploymentEndpointStatus,
    ModelDeploymentRevision,
    ModelDeploymentRuntimePhase,
    ModelDeploymentStatusAvailability,
    ModelDeploymentStatusView,
)
from .models import StrictModel


class PublicationDisposition(StrEnum):
    PUBLISH = "publish"
    WITHDRAW = "withdraw"


class PublicationReason(StrEnum):
    READY = "ready"
    ACTIVATABLE = "activatable"
    STATUS_UNAVAILABLE = "status-unavailable"
    STATUS_STALE = "status-stale"
    SPEC_NOT_OBSERVED = "spec-not-observed"
    DESIRED_STATE_DISABLED = "desired-state-disabled"
    NOT_READY = "not-ready"
    READY_CONDITION_MISSING = "ready-condition-missing"
    ENDPOINT_UNAVAILABLE = "endpoint-unavailable"
    EXPOSURE_DISABLED = "exposure-disabled"
    CATALOG_IDENTITY_CONFLICT = "catalog-identity-conflict"
    ROUTE_BINDING_INVALID = "route-binding-invalid"


PrincipalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9_.:@/-]*[A-Za-z0-9])?$",
    ),
]


class DynamicModelPublication(StrictModel):
    """A Ready-gated candidate; this is not proof that a catalog was written."""

    namespace: str
    name: str
    tenant_id: str
    model_ref: str
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_DIGEST_PATTERN)
    observed_generation: int = Field(ge=1)
    admitted_pool_ref: str
    runtime_profile: str
    runtime_image: str
    artifact_revision: str
    artifact_manifest_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    accelerators_per_replica: int = Field(ge=1, le=64)
    max_queue_seconds: int = Field(ge=1, le=604800)
    endpoint: ModelDeploymentEndpointStatus
    runtime_ready: bool
    open_ai: bool
    open_ai_aliases: list[str] = Field(default_factory=list, max_length=32)
    mcp: bool
    mcp_tool_name: str | None = None
    visibility: Visibility
    policy_ref: str
    allowed_principal_ids: list[PrincipalId] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def valid_exposure(self) -> DynamicModelPublication:
        if len(self.open_ai_aliases) != len(set(self.open_ai_aliases)):
            raise ValueError("OpenAI publication aliases must be unique")
        if not self.open_ai and self.open_ai_aliases:
            raise ValueError("OpenAI aliases require OpenAI publication")
        if self.mcp != (self.mcp_tool_name is not None):
            raise ValueError("MCP publication and tool name must be enabled together")
        if not self.open_ai and not self.mcp:
            raise ValueError("a publication candidate needs at least one protocol")
        if len(self.allowed_principal_ids) != len(set(self.allowed_principal_ids)):
            raise ValueError("publication principal IDs must be unique")
        return self


class ModelPublicationAssessment(StrictModel):
    namespace: str
    name: str
    tenant_id: str
    model_ref: str
    revision: int = Field(ge=1)
    disposition: PublicationDisposition
    reason: PublicationReason
    publication: DynamicModelPublication | None = None

    @model_validator(mode="after")
    def consistent_disposition(self) -> ModelPublicationAssessment:
        if self.disposition is PublicationDisposition.PUBLISH:
            if self.reason not in {PublicationReason.READY, PublicationReason.ACTIVATABLE} or self.publication is None:
                raise ValueError("publish disposition requires a Ready or activatable publication")
        elif self.publication is not None or self.reason in {PublicationReason.READY, PublicationReason.ACTIVATABLE}:
            raise ValueError("withdraw disposition cannot contain a publication")
        return self


class DynamicPublicationSnapshot(StrictModel):
    schema_version: str = "fs2-serve.nebius.ai/dynamic-publication-snapshot/v1"
    assessments: list[ModelPublicationAssessment] = Field(max_length=1024)
    publications: list[DynamicModelPublication] = Field(max_length=1024)
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)


class PublicationPrincipal(StrictModel):
    tenant_id: str
    principal_id: PrincipalId


def _withdraw(revision: ModelDeploymentRevision, reason: PublicationReason) -> ModelPublicationAssessment:
    return ModelPublicationAssessment(
        namespace=revision.namespace,
        name=revision.name,
        tenant_id=revision.tenant_id,
        model_ref=revision.spec.model_ref,
        revision=revision.revision,
        disposition=PublicationDisposition.WITHDRAW,
        reason=reason,
    )


def _phase_condition(status: ModelDeploymentStatusView) -> bool:
    observation = status.observation
    if observation is None:
        return False
    generation = observation.status.observed_generation
    phase = observation.status.phase
    expected = {
        ModelDeploymentRuntimePhase.READY: ModelDeploymentConditionType.READY,
        ModelDeploymentRuntimePhase.COLD: ModelDeploymentConditionType.COLD,
        ModelDeploymentRuntimePhase.RUNTIME_STARTING: ModelDeploymentConditionType.LOADING,
        ModelDeploymentRuntimePhase.WARMING: ModelDeploymentConditionType.LOADING,
        ModelDeploymentRuntimePhase.ADMITTED: ModelDeploymentConditionType.PROGRESSING,
        ModelDeploymentRuntimePhase.NODE_PENDING: ModelDeploymentConditionType.PROGRESSING,
        ModelDeploymentRuntimePhase.LOCALIZING: ModelDeploymentConditionType.PROGRESSING,
    }.get(phase)
    if expected is None:
        return False
    return any(
        condition.type is expected
        and condition.status is KubernetesConditionStatus.TRUE
        and condition.observed_generation == generation
        for condition in observation.status.conditions
    )


def assess_model_publication(
    revision: ModelDeploymentRevision,
    status: ModelDeploymentStatusView | None,
) -> ModelPublicationAssessment:
    """Evaluate one current revision without treating desired state as observed."""

    if revision.spec.lifecycle.desired_state is not DesiredState.ENABLED:
        return _withdraw(revision, PublicationReason.DESIRED_STATE_DISABLED)
    if not revision.spec.exposure.open_ai and not revision.spec.exposure.mcp:
        return _withdraw(revision, PublicationReason.EXPOSURE_DISABLED)
    if status is None or status.state is ModelDeploymentStatusAvailability.UNAVAILABLE:
        return _withdraw(revision, PublicationReason.STATUS_UNAVAILABLE)
    if status.state is ModelDeploymentStatusAvailability.STALE:
        return _withdraw(revision, PublicationReason.STATUS_STALE)
    observation = status.observation
    if observation is None:
        return _withdraw(revision, PublicationReason.STATUS_UNAVAILABLE)
    observed = observation.status
    if (
        status.namespace != revision.namespace
        or status.name != revision.name
        or status.revision != revision.revision
        or observation.namespace != revision.namespace
        or observation.name != revision.name
        or observation.tenant_id != revision.tenant_id
        or observation.revision != revision.revision
        or observed.spec_digest != revision.etag
        or observed.observed_generation < 1
    ):
        return _withdraw(revision, PublicationReason.SPEC_NOT_OBSERVED)
    if observed.phase in {
        ModelDeploymentRuntimePhase.DESIRED,
        ModelDeploymentRuntimePhase.DRAINING,
        ModelDeploymentRuntimePhase.FAILED,
        ModelDeploymentRuntimePhase.INFRASTRUCTURE_REQUIRED,
    }:
        return _withdraw(revision, PublicationReason.NOT_READY)
    if not _phase_condition(status):
        return _withdraw(revision, PublicationReason.READY_CONDITION_MISSING)
    if observed.endpoint is None:
        return _withdraw(revision, PublicationReason.ENDPOINT_UNAVAILABLE)
    if observed.endpoint.namespace != revision.namespace:
        return _withdraw(revision, PublicationReason.ENDPOINT_UNAVAILABLE)
    if observed.admitted_pool_ref is None:
        return _withdraw(revision, PublicationReason.NOT_READY)

    spec = revision.spec
    publication = DynamicModelPublication(
        namespace=revision.namespace,
        name=revision.name,
        tenant_id=revision.tenant_id,
        model_ref=spec.model_ref,
        revision=revision.revision,
        etag=revision.etag,
        observed_generation=observed.observed_generation,
        admitted_pool_ref=observed.admitted_pool_ref,
        runtime_profile=spec.runtime.profile,
        runtime_image=spec.runtime.image,
        artifact_revision=spec.artifact.revision,
        artifact_manifest_digest=spec.artifact.manifest_digest,
        accelerators_per_replica=spec.placement.accelerators_per_replica,
        max_queue_seconds=spec.queue.max_queue_seconds,
        endpoint=observed.endpoint,
        runtime_ready=observed.phase is ModelDeploymentRuntimePhase.READY,
        open_ai=spec.exposure.open_ai,
        open_ai_aliases=sorted(spec.exposure.open_ai_aliases),
        mcp=spec.exposure.mcp,
        mcp_tool_name=spec.exposure.mcp_tool_name,
        visibility=spec.policy.visibility,
        policy_ref=spec.policy.policy_ref,
        allowed_principal_ids=sorted(spec.policy.allowed_principal_ids),
    )
    return ModelPublicationAssessment(
        namespace=revision.namespace,
        name=revision.name,
        tenant_id=revision.tenant_id,
        model_ref=spec.model_ref,
        revision=revision.revision,
        disposition=PublicationDisposition.PUBLISH,
        reason=(
            PublicationReason.READY
            if observed.phase is ModelDeploymentRuntimePhase.READY
            else PublicationReason.ACTIVATABLE
        ),
        publication=publication,
    )


def project_dynamic_publications(
    revisions: Iterable[ModelDeploymentRevision],
    statuses: Mapping[tuple[str, str], ModelDeploymentStatusView],
) -> DynamicPublicationSnapshot:
    """Build one deterministic snapshot and withdraw every ambiguous identity."""

    ordered = sorted(revisions, key=lambda item: (item.namespace, item.name))
    identities = [(item.namespace, item.name) for item in ordered]
    if len(identities) != len(set(identities)):
        raise ValueError("publication input contains duplicate ModelDeployment identities")

    assessments = [assess_model_publication(item, statuses.get((item.namespace, item.name))) for item in ordered]
    conflicts: set[tuple[str, str]] = set()
    open_ai_owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    mcp_owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for assessment in assessments:
        publication = assessment.publication
        if publication is None:
            continue
        identity = (publication.namespace, publication.name)
        if publication.open_ai:
            for model_id in {publication.model_ref, *publication.open_ai_aliases}:
                open_ai_owners[model_id].add(identity)
        if publication.mcp and publication.mcp_tool_name is not None:
            mcp_owners[publication.mcp_tool_name].add(identity)
    for owners in (*open_ai_owners.values(), *mcp_owners.values()):
        if len(owners) > 1:
            conflicts.update(owners)

    resolved = []
    for assessment in assessments:
        identity = (assessment.namespace, assessment.name)
        if assessment.publication is not None and identity in conflicts:
            revision = next(item for item in ordered if (item.namespace, item.name) == identity)
            resolved.append(_withdraw(revision, PublicationReason.CATALOG_IDENTITY_CONFLICT))
        else:
            resolved.append(assessment)
    return _publication_snapshot(resolved)


def _publication_snapshot(
    assessments: Iterable[ModelPublicationAssessment],
) -> DynamicPublicationSnapshot:
    resolved = list(assessments)
    publications = sorted(
        (item.publication for item in resolved if item.publication is not None),
        key=lambda item: (item.tenant_id, item.model_ref, item.namespace, item.name),
    )
    payload = {
        "schema_version": "fs2-serve.nebius.ai/dynamic-publication-snapshot/v1",
        "assessments": [item.model_dump(mode="json") for item in resolved],
        "publications": [item.model_dump(mode="json") for item in publications],
    }
    return DynamicPublicationSnapshot(
        assessments=resolved,
        publications=publications,
        digest=canonical_digest(payload),
    )


def withdraw_invalid_dynamic_publications(
    snapshot: DynamicPublicationSnapshot,
    identities: frozenset[tuple[str, str]],
) -> DynamicPublicationSnapshot:
    """Withdraw exact bind-invalid publications while preserving the inventory.

    The caller must first prove the snapshot inventory is unambiguous. Unknown
    identities are rejected instead of silently changing a different model.
    """

    published = {(item.namespace, item.name) for item in snapshot.assessments if item.publication is not None}
    if not identities.issubset(published):
        raise ValueError("binding rejection identity is absent from the publication snapshot")
    resolved = []
    for item in snapshot.assessments:
        if (item.namespace, item.name) not in identities:
            resolved.append(item)
            continue
        resolved.append(
            ModelPublicationAssessment(
                namespace=item.namespace,
                name=item.name,
                tenant_id=item.tenant_id,
                model_ref=item.model_ref,
                revision=item.revision,
                disposition=PublicationDisposition.WITHDRAW,
                reason=PublicationReason.ROUTE_BINDING_INVALID,
            )
        )
    return _publication_snapshot(resolved)


def publications_for_principal(
    snapshot: DynamicPublicationSnapshot,
    principal: PublicationPrincipal,
) -> list[DynamicModelPublication]:
    """Return only same-tenant publications permitted by explicit policy."""

    permitted = []
    for publication in snapshot.publications:
        if publication.tenant_id != principal.tenant_id:
            continue
        explicitly_allowed = principal.principal_id in publication.allowed_principal_ids
        if publication.visibility is Visibility.PRIVATE and not explicitly_allowed:
            continue
        if publication.allowed_principal_ids and not explicitly_allowed:
            continue
        permitted.append(publication)
    return permitted


class IntegrationSource(StrEnum):
    AUTOSCALER = "autoscaler"
    QUEUE = "queue"
    CACHE = "cache"
    SNAPSHOT = "snapshot"


class IntegrationAvailability(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


IntegrationValue = int | float | str | bool


class ModelIntegrationQuery(StrictModel):
    namespace: str
    name: str
    tenant_id: str
    model_ref: str
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_DIGEST_PATTERN)


class ModelIntegrationObservation(StrictModel):
    source: IntegrationSource
    availability: IntegrationAvailability
    namespace: str
    name: str
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_DIGEST_PATTERN)
    observed_at: AwareDatetime | None = None
    values: dict[str, IntegrationValue] = Field(default_factory=dict, max_length=32)
    reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def truthful_availability(self) -> ModelIntegrationObservation:
        if self.availability is IntegrationAvailability.AVAILABLE:
            if self.observed_at is None or self.reason is not None:
                raise ValueError("available integration data requires observedAt and no reason")
        elif self.reason is None or self.values:
            raise ValueError("stale or unavailable integration data needs a reason and no values")
        return self


class ModelIntegrationAdapter(Protocol):
    source: IntegrationSource

    async def observe(self, query: ModelIntegrationQuery) -> ModelIntegrationObservation: ...


class ModelIntegrationSnapshot(StrictModel):
    query: ModelIntegrationQuery
    observations: list[ModelIntegrationObservation]
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)


def _unavailable_observation(
    query: ModelIntegrationQuery,
    source: IntegrationSource,
    reason: str,
) -> ModelIntegrationObservation:
    return ModelIntegrationObservation(
        source=source,
        availability=IntegrationAvailability.UNAVAILABLE,
        namespace=query.namespace,
        name=query.name,
        revision=query.revision,
        etag=query.etag,
        reason=reason,
    )


async def collect_model_integrations(
    query: ModelIntegrationQuery,
    adapters: Mapping[IntegrationSource, ModelIntegrationAdapter],
) -> ModelIntegrationSnapshot:
    """Collect every bounded source independently and turn failures into unavailable data."""

    async def collect(source: IntegrationSource) -> ModelIntegrationObservation:
        adapter = adapters.get(source)
        if adapter is None:
            return _unavailable_observation(query, source, "adapter-not-configured")
        if adapter.source is not source:
            return _unavailable_observation(query, source, "adapter-source-mismatch")
        try:
            observation = await adapter.observe(query)
        except Exception:  # noqa: BLE001 - an adapter failure must not fail the whole snapshot
            return _unavailable_observation(query, source, "adapter-error")
        if observation.source is not source:
            return _unavailable_observation(query, source, "observation-source-mismatch")
        if (
            observation.namespace != query.namespace
            or observation.name != query.name
            or observation.revision != query.revision
            or observation.etag != query.etag
        ):
            return ModelIntegrationObservation(
                source=source,
                availability=IntegrationAvailability.STALE,
                namespace=query.namespace,
                name=query.name,
                revision=query.revision,
                etag=query.etag,
                reason="observation-identity-stale",
            )
        return observation

    observations = list(await asyncio.gather(*(collect(source) for source in IntegrationSource)))
    observations.sort(key=lambda item: item.source.value)
    payload = {
        "query": query.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
    }
    return ModelIntegrationSnapshot(query=query, observations=observations, digest=canonical_digest(payload))


class DynamicModelMetricSample(StrictModel):
    name: str = Field(pattern=r"^fs2_model_[a-z0-9_]+$")
    labels: dict[str, str] = Field(max_length=3)
    value: float = Field(ge=0)


_NUMERIC_SIGNAL_METRICS: dict[tuple[IntegrationSource, str], str] = {
    (IntegrationSource.AUTOSCALER, "desired_replicas"): "fs2_model_desired_replicas",
    (IntegrationSource.AUTOSCALER, "ready_replicas"): "fs2_model_ready_replicas",
    (IntegrationSource.QUEUE, "queue_depth"): "fs2_model_queue_depth",
    (IntegrationSource.QUEUE, "oldest_seconds"): "fs2_model_queue_oldest_seconds",
    (IntegrationSource.CACHE, "localization_seconds"): "fs2_model_cache_localization_seconds",
    (IntegrationSource.SNAPSHOT, "restore_seconds"): "fs2_model_snapshot_restore_seconds",
    (IntegrationSource.SNAPSHOT, "allocated_accelerators"): "fs2_model_allocated_accelerators",
}


def project_dynamic_model_metrics(
    assessment: ModelPublicationAssessment,
    integrations: ModelIntegrationSnapshot,
) -> list[DynamicModelMetricSample]:
    """Produce bounded samples and omit every unavailable value instead of inventing zero."""

    labels = {"model": assessment.model_ref}
    samples = [
        DynamicModelMetricSample(
            name="fs2_model_publication_eligible",
            labels=labels,
            value=float(assessment.disposition is PublicationDisposition.PUBLISH),
        )
    ]
    for observation in integrations.observations:
        samples.append(
            DynamicModelMetricSample(
                name="fs2_model_integration_source_available",
                labels={**labels, "source": observation.source.value},
                value=float(observation.availability is IntegrationAvailability.AVAILABLE),
            )
        )
        if observation.availability is not IntegrationAvailability.AVAILABLE:
            continue
        for field, value in observation.values.items():
            metric_name = _NUMERIC_SIGNAL_METRICS.get((observation.source, field))
            if metric_name is None or isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                continue
            samples.append(DynamicModelMetricSample(name=metric_name, labels=labels, value=float(value)))
    return samples
