"""Durable records for the experimental ModelDeployment admin API.

These contracts describe PostgreSQL state only.  Appending a desired-state
revision does not write Kubernetes and does not imply that a controller has
observed or applied it.  Runtime observations are stored separately and are
always identified by their own immutable observation ID.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .model_deployment import (
    DNS_LABEL_PATTERN,
    DNS_SUBDOMAIN_PATTERN,
    SHA256_DIGEST_PATTERN,
    ModelDeploymentSpec,
    canonical_digest,
    canonical_json,
    spec_digest,
)
from .models import IdempotencyKey, StrictModel


class ModelDeploymentRevisionAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    ROLLBACK = "rollback"


class ModelDeploymentRuntimePhase(StrEnum):
    DESIRED = "Desired"
    ADMITTED = "Admitted"
    NODE_PENDING = "NodePending"
    LOCALIZING = "Localizing"
    RUNTIME_STARTING = "RuntimeStarting"
    WARMING = "Warming"
    READY = "Ready"
    COLD = "Cold"
    DRAINING = "Draining"
    FAILED = "Failed"
    INFRASTRUCTURE_REQUIRED = "InfrastructureRequired"


class ModelDeploymentCacheState(StrEnum):
    UNKNOWN = "Unknown"
    MISSING = "Missing"
    LOCALIZING = "Localizing"
    CACHED = "Cached"
    FAILED = "Failed"


class ModelDeploymentCacheTier(StrEnum):
    OBJECT_STORE = "ObjectStore"
    SHARED_FILESYSTEM = "SharedFilesystem"
    NODE_LOCAL = "NodeLocal"


class ModelDeploymentAdoptionState(StrEnum):
    NONE = "None"
    OBSERVE_ONLY = "ObserveOnly"
    CLAIMING = "Claiming"
    OWNED = "Owned"
    ROLLING_BACK = "RollingBack"
    FAILED = "Failed"


class ModelDeploymentConditionType(StrEnum):
    READY = "Ready"
    CACHED = "Cached"
    COLD = "Cold"
    LOADING = "Loading"
    DRAINING = "Draining"
    INFRASTRUCTURE_REQUIRED = "InfrastructureRequired"
    FAILED = "Failed"
    PROGRESSING = "Progressing"


class KubernetesConditionStatus(StrEnum):
    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class ModelDeploymentReplicaStatus(StrictModel):
    desired: int | None = Field(default=None, ge=0)
    admitted: int | None = Field(default=None, ge=0)
    node_pending: int | None = Field(default=None, ge=0)
    localizing: int | None = Field(default=None, ge=0)
    runtime_starting: int | None = Field(default=None, ge=0)
    warming: int | None = Field(default=None, ge=0)
    ready: int | None = Field(default=None, ge=0)
    available: int | None = Field(default=None, ge=0)


class ModelDeploymentCacheStatus(StrictModel):
    state: ModelDeploymentCacheState
    tier: ModelDeploymentCacheTier | None = None
    digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    observed_at: AwareDatetime | None = None


class ModelDeploymentPublicationStatus(StrictModel):
    open_ai: bool
    mcp: bool
    observed_at: AwareDatetime | None = None


class ModelDeploymentAdoptionStatus(StrictModel):
    state: ModelDeploymentAdoptionState
    receipt_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)


class ModelDeploymentResourceStatus(StrictModel):
    identity: str = Field(min_length=1, max_length=512)
    api_version: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    uid: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)


class ModelDeploymentInfrastructureHandoff(StrictModel):
    reason: str = Field(min_length=1, max_length=240)
    owner: Literal["Terraform"] = "Terraform"
    required_inputs: list[
        Annotated[
            str,
            StringConstraints(
                min_length=1,
                max_length=128,
                pattern=r"^[a-z][a-z0-9._-]*$",
            ),
        ]
    ] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_inputs(self) -> ModelDeploymentInfrastructureHandoff:
        if len(self.required_inputs) != len(set(self.required_inputs)):
            raise ValueError("infrastructure handoff inputs must be unique")
        return self


class ModelDeploymentCondition(StrictModel):
    type: ModelDeploymentConditionType
    status: KubernetesConditionStatus
    observed_generation: int = Field(ge=0)
    reason: str = Field(
        min_length=1,
        max_length=1024,
        pattern=r"^[A-Za-z](?:[A-Za-z0-9_,:]*[A-Za-z0-9_])?$",
    )
    message: str = Field(min_length=1, max_length=32768)
    last_transition_time: AwareDatetime


class ModelDeploymentObservedStatus(StrictModel):
    """Strict, bounded projection of the CR status contract."""

    observed_generation: int = Field(ge=0)
    phase: ModelDeploymentRuntimePhase
    spec_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    render_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    active_revision: str | None = Field(default=None, min_length=1, max_length=256)
    admitted_pool_ref: str | None = Field(default=None, min_length=1, max_length=128)
    replicas: ModelDeploymentReplicaStatus | None = None
    cache: ModelDeploymentCacheStatus | None = None
    publication: ModelDeploymentPublicationStatus | None = None
    adoption: ModelDeploymentAdoptionStatus | None = None
    resources: list[ModelDeploymentResourceStatus] = Field(default_factory=list, max_length=256)
    infrastructure_handoff: ModelDeploymentInfrastructureHandoff | None = None
    retry_count: int = Field(default=0, ge=0, le=20)
    last_reconcile_time: AwareDatetime
    conditions: list[ModelDeploymentCondition] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_status_maps(self) -> ModelDeploymentObservedStatus:
        if len({item.identity for item in self.resources}) != len(self.resources):
            raise ValueError("resource status identities must be unique")
        if len({item.type for item in self.conditions}) != len(self.conditions):
            raise ValueError("condition types must be unique")
        return self


class ModelDeploymentRevision(StrictModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_DIGEST_PATTERN)
    spec: ModelDeploymentSpec
    action: ModelDeploymentRevisionAction
    created_at: AwareDatetime
    created_by: str = Field(min_length=1, max_length=200)
    previous_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def consistent_identity(self) -> ModelDeploymentRevision:
        if self.spec.tenant_id != self.tenant_id:
            raise ValueError("revision tenant must match the desired spec")
        if self.etag != spec_digest(self.spec):
            raise ValueError("revision ETag must match the canonical desired spec")
        if self.revision == 1 and self.previous_revision is not None:
            raise ValueError("initial revision cannot have a predecessor")
        if self.revision == 1 and self.action is not ModelDeploymentRevisionAction.CREATE:
            raise ValueError("initial revision must be a create action")
        if self.revision > 1 and self.previous_revision != self.revision - 1:
            raise ValueError("revision predecessor must be contiguous")
        if self.revision > 1 and self.action is ModelDeploymentRevisionAction.CREATE:
            raise ValueError("a later revision cannot be a create action")
        return self


class ModelDeploymentAppendRequest(StrictModel):
    """Internal repository command; it is deliberately not an HTTP body."""

    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    expected_etag: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    spec: ModelDeploymentSpec
    action: ModelDeploymentRevisionAction
    actor_id: UUID
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: IdempotencyKey


class ModelDeploymentAppendResult(StrictModel):
    value: ModelDeploymentRevision
    reused: bool = False


def model_deployment_append_payload(request: ModelDeploymentAppendRequest) -> bytes:
    """Canonical request identity used only for keyed replay comparison."""

    return canonical_json(
        {
            "namespace": request.namespace,
            "name": request.name,
            "expected_etag": request.expected_etag,
            "spec": request.spec.model_dump(mode="json", by_alias=True),
            "action": request.action.value,
        }
    )


def model_deployment_audit_target(namespace: str, name: str) -> str:
    """Bound the audit target without losing collision resistance."""

    value = f"{namespace}/{name}"
    if len(value) <= 200:
        return value
    return canonical_digest({"namespace": namespace, "name": name})


class ModelDeploymentStatusObservation(StrictModel):
    observation_id: UUID
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    revision: int = Field(ge=1)
    status: ModelDeploymentObservedStatus
    observed_at: AwareDatetime


class ModelDeploymentList(StrictModel):
    items: list[ModelDeploymentRevision] = Field(max_length=200)
    next_after: str | None = Field(default=None, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)


class ModelDeploymentHistory(StrictModel):
    items: list[ModelDeploymentRevision] = Field(max_length=200)
    next_before_revision: int | None = Field(default=None, ge=1)


class ModelDeploymentStatusAvailability(StrEnum):
    OBSERVED = "observed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ModelDeploymentStatusView(StrictModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_DIGEST_PATTERN)
    state: ModelDeploymentStatusAvailability
    observation: ModelDeploymentStatusObservation | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def consistent_availability(self) -> ModelDeploymentStatusView:
        if self.state is ModelDeploymentStatusAvailability.OBSERVED:
            if self.observation is None or self.reason is not None:
                raise ValueError("observed status requires an observation and no reason")
        elif self.state is ModelDeploymentStatusAvailability.STALE:
            if self.observation is None or self.reason is None:
                raise ValueError("stale status requires an observation and reason")
        elif self.observation is not None or self.reason is None:
            raise ValueError("unavailable status requires a reason and no observation")
        return self
