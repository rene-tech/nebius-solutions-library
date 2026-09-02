"""Runtime-neutral ModelDeployment contracts and pure reconciliation planning.

This module has no Kubernetes client and performs no writes.  It provides the
same fail-closed validation and deterministic renderer contract to the admin
planner and the eventual controller process.  A Kubernetes adapter is allowed
to apply a plan only when its disposition is ``accepted``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter
from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .fast_start import (
    FastStartAssessment,
    FastStartMode,
    FastStartQualificationState,
    FastStartSpec,
    evaluate_fast_start,
)
from .models import KubernetesModel

API_VERSION = "inference.fs2.nebius.ai/v1alpha1"
KIND = "ModelDeployment"
FINALIZER = "inference.fs2.nebius.ai/model-cleanup"
FIELD_MANAGER = "fs2-model-controller"
SPEC_DIGEST_ANNOTATION = "fs2-serve.nebius.ai/spec-digest"
MODEL_DEPLOYMENT_LABEL = "fs2-serve.nebius.ai/model-deployment"
MODEL_ID_LABEL = "fs2-serve.nebius.ai/model-id"
KUEUE_QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
KUEUE_PRIORITY_LABEL = "kueue.x-k8s.io/priority-class"
EFFECTIVE_HOT_FLOOR_ANNOTATION = "fs2-serve.nebius.ai/effective-hot-floor"
WORKLOAD_POOL_ANNOTATION = "fs2-serve.nebius.ai/workload-pool-ref"
WORKLOAD_ROLE_ANNOTATION = "fs2-serve.nebius.ai/workload-role"
WORKLOAD_SEGMENT_CAPACITY_ANNOTATION = "fs2-serve.nebius.ai/workload-segment-capacity"
WORKLOAD_SEGMENT_OFFSET_ANNOTATION = "fs2-serve.nebius.ai/workload-segment-offset"
WORKLOAD_ROLE_LABEL = "fs2-serve.nebius.ai/workload-role"
POOL_ID_NODE_LABEL = "accelerator.fs2.nebius/pool-id"

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
DNS_LABEL_PATTERN = r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"
DNS_SUBDOMAIN_PATTERN = (
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$"
)
MODEL_REF_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$"
SUPPORTED_DYNAMIC_POLICY_REF = "tenant-default.v1"
IMAGE_DIGEST_PATTERN = r"^[^\s@]+@sha256:[a-f0-9]{64}$"

PoolRef = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)]
OpenAIAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]*[A-Za-z0-9])?$",
    ),
]
PrincipalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9_.:@/-]*[A-Za-z0-9])?$",
    ),
]


class DesiredState(StrEnum):
    ENABLED = "Enabled"
    DRAINING = "Draining"
    DISABLED = "Disabled"


class TopologyPolicy(StrEnum):
    ANY = "Any"
    SINGLE_NODE = "SingleNode"
    HIGH_BANDWIDTH_DOMAIN = "HighBandwidthDomain"


class CacheTier(StrEnum):
    DISABLED = "Disabled"
    OBJECT_STORE = "ObjectStore"
    SHARED_FILESYSTEM = "SharedFilesystem"
    NODE_LOCAL = "NodeLocal"


class SnapshotPreference(StrEnum):
    NEVER = "Never"
    PREFER = "Prefer"
    REQUIRE = "Require"


class SnapshotStrategy(StrEnum):
    WEIGHTS = "Weights"
    RUNTIME_NATIVE = "RuntimeNative"
    CUDA_CHECKPOINT = "CudaCheckpoint"


class RolloutStrategy(StrEnum):
    ROLLING = "Rolling"
    RECREATE = "Recreate"


class Visibility(StrEnum):
    PRIVATE = "Private"
    TENANT = "Tenant"


class AdoptionMode(StrEnum):
    NONE = "None"
    OBSERVE = "Observe"
    CLAIM = "Claim"


class NamedDigest(KubernetesModel):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)


class ArtifactStorageRef(KubernetesModel):
    kind: Literal["ObjectStore", "PersistentVolumeClaim", "LocalModelCache"]
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)


class ArtifactSpec(KubernetesModel):
    revision: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$")
    manifest_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    storage_ref: ArtifactStorageRef | None = None


class RuntimeSpec(KubernetesModel):
    profile: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    image: str = Field(min_length=73, max_length=768, pattern=IMAGE_DIGEST_PATTERN)
    template_ref: NamedDigest


class LifecycleSpec(KubernetesModel):
    desired_state: DesiredState


class PlacementSpec(KubernetesModel):
    pool_refs: list[PoolRef] = Field(min_length=1, max_length=32)
    accelerators_per_replica: int = Field(ge=1, le=64)
    topology_policy: TopologyPolicy

    @model_validator(mode="after")
    def unique_pools(self) -> PlacementSpec:
        if len(self.pool_refs) != len(set(self.pool_refs)):
            raise ValueError("placement poolRefs must be unique")
        if any(len(value) > 128 or not value for value in self.pool_refs):
            raise ValueError("placement poolRef is outside the accepted bound")
        return self


class WarmWindowSpec(KubernetesModel):
    name: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    schedule: str = Field(min_length=9, max_length=128)
    time_zone: str = Field(min_length=1, max_length=64)
    duration_seconds: int = Field(ge=60, le=604800)
    min_replicas: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def valid_schedule(self) -> WarmWindowSpec:
        try:
            zone = ZoneInfo(self.time_zone)
        except ZoneInfoNotFoundError:
            raise ValueError("warm window timeZone must be an IANA time zone") from None
        try:
            croniter(self.schedule, datetime.now(zone)).get_prev(datetime)
        except (CroniterBadCronError, KeyError, TypeError, ValueError):
            raise ValueError("warm window schedule must be a valid cron expression") from None
        return self


class AvailabilitySpec(KubernetesModel):
    min_replicas: int = Field(ge=0, le=10000)
    max_replicas: int = Field(ge=0, le=10000)
    idle_seconds: int = Field(ge=0, le=604800)
    target_queue_depth: int = Field(ge=1, le=100000)
    polling_interval_seconds: int = Field(ge=1, le=60)
    cooldown_seconds: int = Field(ge=5, le=86400)
    warm_windows: list[WarmWindowSpec] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_bounds(self) -> AvailabilitySpec:
        if self.max_replicas < self.min_replicas:
            raise ValueError("maxReplicas must be greater than or equal to minReplicas")
        if len({window.name for window in self.warm_windows}) != len(self.warm_windows):
            raise ValueError("warm window names must be unique")
        if any(window.min_replicas > self.max_replicas for window in self.warm_windows):
            raise ValueError("warm window minReplicas cannot exceed maxReplicas")
        return self


class SnapshotRef(NamedDigest):
    strategy: SnapshotStrategy


class CacheSpec(KubernetesModel):
    tier: CacheTier
    snapshot_preference: SnapshotPreference
    snapshot_ref: SnapshotRef | None = None

    @model_validator(mode="after")
    def valid_snapshot(self) -> CacheSpec:
        if self.snapshot_preference is SnapshotPreference.NEVER and self.snapshot_ref is not None:
            raise ValueError("snapshotRef must be absent when snapshotPreference is Never")
        if self.snapshot_preference is not SnapshotPreference.NEVER and self.snapshot_ref is None:
            raise ValueError("Prefer or Require snapshot policy needs a qualified snapshotRef")
        if self.snapshot_preference is SnapshotPreference.REQUIRE and self.tier is CacheTier.DISABLED:
            raise ValueError("a required snapshot needs an enabled cache tier")
        return self


class FastStartSample(KubernetesModel):
    """One benchmark attempt.

    ``modelStartSeconds`` is measured from GPU capacity being available until
    semantic endpoint readiness; ``None`` means the attempt never became
    semantically ready.  Capacity wait and total end-to-end time are separate
    measurements and may be absent.
    """

    observed_at: AwareDatetime
    model_start_seconds: float | None = Field(default=None, ge=0, le=86400)
    capacity_wait_seconds: float | None = Field(default=None, ge=0, le=604800)
    end_to_end_seconds: float | None = Field(default=None, ge=0, le=604800)

    @model_validator(mode="after")
    def consistent_phases(self) -> FastStartSample:
        if self.end_to_end_seconds is not None:
            for phase in (self.model_start_seconds, self.capacity_wait_seconds):
                if phase is not None and phase > self.end_to_end_seconds:
                    raise ValueError("end-to-end seconds cannot be shorter than one of its phases")
        return self


class FastStartEvidence(KubernetesModel):
    """Retained benchmark evidence for one exact runtime tuple.

    The mechanism is descriptive operator detail.  Compatibility with a
    ModelDeployment is decided only by the exact digests, cache tier, snapshot,
    accelerator class, and accelerator count recorded here.
    """

    receipt_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    mechanism: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    compatibility_tuple_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    compatibility_tuple_complete: bool
    measurement_basis: Literal["CapacityAvailableToSemanticReady"]
    accelerator_class: str = Field(min_length=1, max_length=128)
    pool_ref: PoolRef | None = None
    accelerators_per_replica: int = Field(ge=1, le=64)
    artifact_manifest_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    runtime_image: str = Field(min_length=73, max_length=768, pattern=IMAGE_DIGEST_PATTERN)
    template_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    cache_tier: CacheTier
    snapshot_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    samples: list[FastStartSample] = Field(min_length=1, max_length=256)
    valid_until: AwareDatetime | None = None


class QueueSpec(KubernetesModel):
    local_queue: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    priority_class: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    max_queue_seconds: int = Field(ge=1, le=604800)


class RolloutSpec(KubernetesModel):
    strategy: RolloutStrategy
    max_unavailable: int = Field(ge=0, le=10000)
    max_surge: int = Field(ge=0, le=10000)
    progress_deadline_seconds: int = Field(ge=60, le=86400)

    @model_validator(mode="after")
    def valid_strategy(self) -> RolloutSpec:
        if self.strategy is RolloutStrategy.ROLLING and self.max_unavailable + self.max_surge == 0:
            raise ValueError("a rolling rollout must allow unavailability or surge")
        if self.strategy is RolloutStrategy.RECREATE and (self.max_unavailable, self.max_surge) != (1, 0):
            raise ValueError("Recreate rollouts use maxUnavailable=1 and maxSurge=0")
        return self


class ExposureSpec(KubernetesModel):
    open_ai: bool = Field(alias="openAI")
    open_ai_aliases: list[OpenAIAlias] = Field(default_factory=list, max_length=32, alias="openAIAliases")
    mcp: bool
    mcp_tool_name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def valid_exposure(self) -> ExposureSpec:
        if len(self.open_ai_aliases) != len(set(self.open_ai_aliases)):
            raise ValueError("OpenAI aliases must be unique")
        if not self.open_ai and self.open_ai_aliases:
            raise ValueError("OpenAI aliases require OpenAI exposure")
        if self.mcp != (self.mcp_tool_name is not None):
            raise ValueError("MCP exposure and mcpToolName must be enabled together")
        return self


class TenantPolicySpec(KubernetesModel):
    visibility: Visibility
    policy_ref: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    allowed_principal_ids: list[PrincipalId] = Field(default_factory=list, max_length=256)
    rate_policy_ref: str | None = Field(default=None, min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)

    @model_validator(mode="after")
    def unique_principals(self) -> TenantPolicySpec:
        if len(self.allowed_principal_ids) != len(set(self.allowed_principal_ids)):
            raise ValueError("allowed principal IDs must be unique")
        return self


class AdoptionSpec(KubernetesModel):
    mode: AdoptionMode = AdoptionMode.NONE
    receipt_ref: NamedDigest | None = None

    @model_validator(mode="after")
    def valid_receipt(self) -> AdoptionSpec:
        if self.mode is AdoptionMode.CLAIM and self.receipt_ref is None:
            raise ValueError("Claim adoption requires a receiptRef")
        if self.mode is not AdoptionMode.CLAIM and self.receipt_ref is not None:
            raise ValueError("receiptRef is valid only for Claim adoption")
        return self


class ModelDeploymentSpec(KubernetesModel):
    model_ref: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
    lifecycle: LifecycleSpec
    artifact: ArtifactSpec
    runtime: RuntimeSpec
    placement: PlacementSpec
    availability: AvailabilitySpec
    cache: CacheSpec
    queue: QueueSpec
    rollout: RolloutSpec
    exposure: ExposureSpec
    policy: TenantPolicySpec
    adoption: AdoptionSpec = Field(default_factory=AdoptionSpec)
    fast_start: FastStartSpec = Field(default_factory=FastStartSpec)

    @model_validator(mode="after")
    def valid_lifecycle(self) -> ModelDeploymentSpec:
        if self.lifecycle.desired_state is not DesiredState.ENABLED and self.availability.min_replicas != 0:
            raise ValueError("a disabled or draining model must have a zero hot floor")
        if self.lifecycle.desired_state is DesiredState.ENABLED and self.availability.max_replicas == 0:
            raise ValueError("an enabled model must permit at least one replica")
        return self


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def spec_digest(spec: ModelDeploymentSpec) -> str:
    payload = spec.model_dump(mode="json", by_alias=True)
    payload["placement"]["poolRefs"] = sorted(payload["placement"]["poolRefs"])
    payload["availability"]["warmWindows"] = sorted(
        payload["availability"]["warmWindows"], key=lambda item: item["name"]
    )
    payload["exposure"]["openAIAliases"] = sorted(payload["exposure"]["openAIAliases"])
    payload["policy"]["allowedPrincipalIds"] = sorted(payload["policy"]["allowedPrincipalIds"])
    if spec.fast_start == FastStartSpec():
        # The optional fast-start policy joined the contract after revisions
        # were already persisted; an unset or default policy keeps every
        # existing ETag and controller spec-digest annotation stable.
        del payload["fastStart"]
    return canonical_digest(payload)


class ValidationDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INFRASTRUCTURE_REQUIRED = "infrastructure-required"


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(KubernetesModel):
    severity: ValidationSeverity
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    path: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=240)
    owner: Literal["live-control-plane", "terraform"]


class ValidationDecision(KubernetesModel):
    disposition: ValidationDisposition
    spec_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    issues: list[ValidationIssue] = Field(max_length=256)
    terraform_inputs: list[str] = Field(default_factory=list, max_length=64)
    admitted_pool_ref: str | None = Field(default=None, min_length=1, max_length=128)
    fast_start: FastStartAssessment | None = None

    @model_validator(mode="after")
    def consistent_disposition(self) -> ValidationDecision:
        error_owners = {issue.owner for issue in self.issues if issue.severity is ValidationSeverity.ERROR}
        if self.disposition is ValidationDisposition.ACCEPTED and error_owners:
            raise ValueError("accepted validation cannot contain errors")
        if self.disposition is ValidationDisposition.REJECTED and "live-control-plane" not in error_owners:
            raise ValueError("rejected validation needs a live-control-plane error")
        if self.disposition is ValidationDisposition.INFRASTRUCTURE_REQUIRED and (
            not self.terraform_inputs or "terraform" not in error_owners or "live-control-plane" in error_owners
        ):
            raise ValueError("infrastructure-required validation needs only Terraform-owned errors and inputs")
        return self


class PoolEnvelope(KubernetesModel):
    pool_id: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    accelerator_class: str = Field(min_length=1, max_length=128)
    resource_name: str = Field(min_length=1, max_length=253)
    capacity_type: Literal["regular", "preemptible"]
    accelerators_per_node: int = Field(ge=1, le=64)
    min_nodes: int = Field(ge=0, le=10000)
    max_nodes: int = Field(ge=0, le=10000)
    node_selector: dict[str, str] = Field(min_length=1, max_length=32)
    tolerations: list[dict[str, str | int]] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_capacity(self) -> PoolEnvelope:
        if self.max_nodes < self.min_nodes:
            raise ValueError("pool maxNodes must be greater than or equal to minNodes")
        if self.node_selector.get(POOL_ID_NODE_LABEL) != self.pool_id:
            raise ValueError("pool nodeSelector must contain its exact accelerator pool identity")
        return self


class ModelQualification(KubernetesModel):
    model_ref: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    runtime_profile: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    artifact_revisions: dict[str, str] = Field(min_length=1, max_length=64)
    artifact_manifest_digests: list[str] = Field(min_length=1, max_length=64)
    runtime_images: list[str] = Field(min_length=1, max_length=64)
    accelerator_classes: list[str] = Field(min_length=1, max_length=128)
    max_accelerators_per_replica: int = Field(ge=1, le=64)
    template_digests: list[str] = Field(min_length=1, max_length=64)
    template_refs: dict[str, str] = Field(min_length=1, max_length=64)
    template_cache_tiers: dict[str, CacheTier] = Field(min_length=1, max_length=64)
    open_ai_qualified: bool = Field(alias="openAIQualified")
    mcp_tool_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    snapshot_digests: list[str] = Field(default_factory=list, max_length=64)
    scale_to_zero_qualified: bool
    fast_start_evidence: list[FastStartEvidence] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def exact_artifacts(self) -> ModelQualification:
        revision_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$")
        if any(
            len(revision) > 256 or revision_pattern.fullmatch(revision) is None for revision in self.artifact_revisions
        ):
            raise ValueError("qualification artifact revision is invalid")
        if any(re.fullmatch(SHA256_DIGEST_PATTERN, digest) is None for digest in self.artifact_revisions.values()):
            raise ValueError("qualification artifact digest is invalid")
        if set(self.artifact_revisions.values()) != set(self.artifact_manifest_digests):
            raise ValueError("qualified artifact revisions and manifest digests must describe the same set")
        if (
            len(set(self.template_digests)) != len(self.template_digests)
            or len(set(self.template_refs.values())) != len(self.template_refs)
            or set(self.template_refs.values()) != set(self.template_digests)
            or any(len(name) > 253 or re.fullmatch(DNS_SUBDOMAIN_PATTERN, name) is None for name in self.template_refs)
            or any(re.fullmatch(SHA256_DIGEST_PATTERN, digest) is None for digest in self.template_refs.values())
        ):
            raise ValueError("qualified template names and digests must form one exact mapping")
        if set(self.template_cache_tiers) != set(self.template_digests):
            raise ValueError("every qualified template digest must have one exact cache tier")
        if len(set(self.snapshot_digests)) != len(self.snapshot_digests) or any(
            re.fullmatch(SHA256_DIGEST_PATTERN, digest) is None for digest in self.snapshot_digests
        ):
            raise ValueError("qualification snapshot digests must be unique SHA-256 identities")
        return self


class InfrastructureEnvelope(KubernetesModel):
    revision: str = Field(pattern=SHA256_DIGEST_PATTERN)
    pools: dict[str, PoolEnvelope] = Field(min_length=1, max_length=128)
    qualifications: dict[str, ModelQualification] = Field(min_length=1, max_length=512)
    local_queues: list[str] = Field(min_length=1, max_length=128)
    priority_classes: list[str] = Field(min_length=1, max_length=128)
    tenant_ids: list[str] = Field(min_length=1, max_length=1024)
    max_accelerators_per_model: int = Field(default=1024, ge=1, le=640000)
    fast_start_wait_second_value: float = Field(default=0.01, ge=0, le=1000000)
    fast_start_mechanism_hourly_costs: dict[str, float] = Field(default_factory=dict, max_length=128)

    @model_validator(mode="after")
    def key_identities_match(self) -> InfrastructureEnvelope:
        if any(key != pool.pool_id for key, pool in self.pools.items()):
            raise ValueError("pool map key must match poolId")
        if any(key != item.model_ref for key, item in self.qualifications.items()):
            raise ValueError("qualification map key must match modelRef")
        if any(
            re.fullmatch(r"^[a-z][a-z0-9-]{0,63}$", name) is None or not math.isfinite(cost) or cost < 0
            for name, cost in self.fast_start_mechanism_hourly_costs.items()
        ):
            raise ValueError("fast-start mechanism costs must map bounded names to finite non-negative values")
        return self


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    owner: Literal["live-control-plane", "terraform"],
) -> ValidationIssue:
    return ValidationIssue(
        severity=ValidationSeverity.ERROR,
        code=code,
        path=path,
        message=message,
        owner=owner,
    )


def validate_model_deployment(
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope,
    *,
    current: ModelDeploymentSpec | None = None,
    evaluation_time: datetime | None = None,
) -> ValidationDecision:
    """Validate one revision before any renderer, Kubernetes, or cloud action."""

    issues: list[ValidationIssue] = []
    terraform_inputs: set[str] = set()
    fast_start = evaluate_fast_start(spec, envelope, evaluation_time=evaluation_time or datetime.now(UTC))
    if fast_start.qualification.state is FastStartQualificationState.UNQUALIFIED:
        issues.append(
            _issue(
                "fast_start_target_unqualified",
                "$.spec.fastStart.level"
                if spec.fast_start.mode is FastStartMode.FIXED
                else "$.spec.fastStart.minimumLevel",
                fast_start.qualification.message,
                owner="live-control-plane",
            )
        )

    if current is not None:
        immutable = (
            ("modelRef", current.model_ref, spec.model_ref),
            ("tenantId", current.tenant_id, spec.tenant_id),
            ("runtime.profile", current.runtime.profile, spec.runtime.profile),
        )
        for path, before, after in immutable:
            if before != after:
                issues.append(
                    _issue(
                        "immutable_identity_changed",
                        f"$.spec.{path}",
                        f"{path} is immutable; create a separate ModelDeployment",
                        owner="live-control-plane",
                    )
                )

    if spec.policy.policy_ref != SUPPORTED_DYNAMIC_POLICY_REF:
        issues.append(
            _issue(
                "policy_ref_unsupported",
                "$.spec.policy.policyRef",
                "this controller version implements only tenant-default.v1",
                owner="live-control-plane",
            )
        )
    if spec.policy.rate_policy_ref is not None:
        issues.append(
            _issue(
                "rate_policy_ref_unsupported",
                "$.spec.policy.ratePolicyRef",
                "model-specific rate policies are not implemented; use API-key request and concurrency limits",
                owner="live-control-plane",
            )
        )

    if spec.artifact.storage_ref is not None:
        issues.append(
            _issue(
                "artifact_storage_ref_unsupported",
                "$.spec.artifact.storageRef",
                "external artifact storage references are not rendered by legacy-manifest-v1; omit storageRef",
                owner="live-control-plane",
            )
        )
    if spec.placement.topology_policy is TopologyPolicy.HIGH_BANDWIDTH_DOMAIN:
        issues.append(
            _issue(
                "topology_policy_unsupported",
                "$.spec.placement.topologyPolicy",
                "HighBandwidthDomain placement is not rendered by legacy-manifest-v1; use SingleNode or Any",
                owner="live-control-plane",
            )
        )
    if spec.cache.snapshot_preference is not SnapshotPreference.NEVER:
        issues.append(
            _issue(
                "snapshot_restore_unsupported",
                "$.spec.cache.snapshotPreference",
                "snapshot restore is not rendered by legacy-manifest-v1; use Never and omit snapshotRef",
                owner="live-control-plane",
            )
        )

    canonical_model_ids = set(envelope.qualifications)
    for index, alias in enumerate(spec.exposure.open_ai_aliases):
        if alias in canonical_model_ids:
            issues.append(
                _issue(
                    "openai_alias_conflicts_catalog",
                    f"$.spec.exposure.openAIAliases[{index}]",
                    "OpenAI alias collides with a canonical catalog model ID; omit it or choose a distinct alias",
                    owner="live-control-plane",
                )
            )

    qualification = envelope.qualifications.get(spec.model_ref)
    if qualification is None:
        issues.append(
            _issue(
                "unknown_model",
                "$.spec.modelRef",
                "modelRef is absent from the installed qualified catalog",
                owner="live-control-plane",
            )
        )
    else:
        if qualification.runtime_profile != spec.runtime.profile:
            issues.append(
                _issue(
                    "runtime_profile_unqualified",
                    "$.spec.runtime.profile",
                    "runtime profile is not qualified for this model",
                    owner="live-control-plane",
                )
            )
        if qualification.artifact_revisions.get(spec.artifact.revision) != spec.artifact.manifest_digest:
            issues.append(
                _issue(
                    "artifact_revision_unqualified",
                    "$.spec.artifact",
                    "artifact revision and manifest digest are not an exact qualified pair",
                    owner="live-control-plane",
                )
            )
        if (
            spec.lifecycle.desired_state is DesiredState.ENABLED
            and spec.availability.min_replicas == 0
            and not qualification.scale_to_zero_qualified
        ):
            issues.append(
                _issue(
                    "scale_to_zero_unqualified",
                    "$.spec.availability.minReplicas",
                    "scale-to-zero is not qualified for this model/runtime tuple",
                    owner="live-control-plane",
                )
            )
        if spec.runtime.image not in qualification.runtime_images:
            issues.append(
                _issue(
                    "runtime_image_unqualified",
                    "$.spec.runtime.image",
                    "runtime image digest is not qualified for this model",
                    owner="live-control-plane",
                )
            )
        if spec.exposure.open_ai and not qualification.open_ai_qualified:
            issues.append(
                _issue(
                    "openai_exposure_unqualified",
                    "$.spec.exposure.openAI",
                    "OpenAI exposure is not qualified for this model runtime",
                    owner="live-control-plane",
                )
            )
        if spec.exposure.mcp and qualification.mcp_tool_name is None:
            issues.append(
                _issue(
                    "mcp_exposure_unqualified",
                    "$.spec.exposure.mcp",
                    "MCP exposure is not qualified for this model runtime",
                    owner="live-control-plane",
                )
            )
        elif spec.exposure.mcp and spec.exposure.mcp_tool_name != qualification.mcp_tool_name:
            issues.append(
                _issue(
                    "mcp_tool_name_unqualified",
                    "$.spec.exposure.mcpToolName",
                    "MCP tool name differs from the exact qualified catalog identity",
                    owner="live-control-plane",
                )
            )
        if qualification.template_refs.get(spec.runtime.template_ref.name) != spec.runtime.template_ref.digest:
            issues.append(
                _issue(
                    "template_ref_unqualified",
                    "$.spec.runtime.templateRef",
                    "runtime template name and digest are not an exact qualified pair",
                    owner="live-control-plane",
                )
            )
        elif qualification.template_cache_tiers[spec.runtime.template_ref.digest] is not spec.cache.tier:
            issues.append(
                _issue(
                    "cache_tier_unqualified",
                    "$.spec.cache.tier",
                    "cache tier does not match the exact qualified runtime template",
                    owner="live-control-plane",
                )
            )
        if spec.placement.accelerators_per_replica > qualification.max_accelerators_per_replica:
            issues.append(
                _issue(
                    "accelerator_count_unqualified",
                    "$.spec.placement.acceleratorsPerReplica",
                    "accelerator count exceeds the model qualification",
                    owner="live-control-plane",
                )
            )
        if spec.cache.snapshot_ref is not None and spec.cache.snapshot_ref.digest not in qualification.snapshot_digests:
            issues.append(
                _issue(
                    "snapshot_unqualified",
                    "$.spec.cache.snapshotRef.digest",
                    "snapshot digest is not qualified for this model/runtime placement",
                    owner="live-control-plane",
                )
            )

    known_pools: list[PoolEnvelope] = []
    for index, pool_ref in enumerate(spec.placement.pool_refs):
        pool = envelope.pools.get(pool_ref)
        if pool is None:
            issues.append(
                _issue(
                    "pool_infrastructure_required",
                    f"$.spec.placement.poolRefs[{index}]",
                    "pool is not present in the Terraform-owned infrastructure envelope",
                    owner="terraform",
                )
            )
            terraform_inputs.add(f"accelerator_pools.{pool_ref}")
            continue
        known_pools.append(pool)
        if spec.placement.accelerators_per_replica > pool.accelerators_per_node:
            issues.append(
                _issue(
                    "accelerator_shape_incompatible",
                    f"$.spec.placement.poolRefs[{index}]",
                    "replica cannot fit on one node in this pool",
                    owner="live-control-plane",
                )
            )
        if qualification is not None and pool.accelerator_class not in qualification.accelerator_classes:
            issues.append(
                _issue(
                    "accelerator_placement_unqualified",
                    f"$.spec.placement.poolRefs[{index}]",
                    "pool accelerator class is not qualified for this model/runtime",
                    owner="live-control-plane",
                )
            )

    pool_replica_capacity = {
        pool.pool_id: (pool.accelerators_per_node // spec.placement.accelerators_per_replica) * pool.max_nodes
        for pool in known_pools
    }
    # Every admitted pool becomes an independently bounded workload segment.
    # Summing is safe here: the renderer never asks two autoscalers to own the
    # same Deployment and each segment is pinned to exactly one pool.
    possible_replicas = sum(pool_replica_capacity.values())
    if known_pools and spec.availability.max_replicas > possible_replicas:
        issues.append(
            _issue(
                "pool_capacity_infrastructure_required",
                "$.spec.availability.maxReplicas",
                "requested maximum exceeds the Terraform-owned pool envelopes",
                owner="terraform",
            )
        )
        terraform_inputs.update(f"accelerator_pools.{pool.pool_id}.max_nodes" for pool in known_pools)

    # If the policy selects durable and preemptible capacity together, every
    # configured hot floor (including a future warm window) must fit entirely
    # on the durable side. Otherwise the label "hot" would falsely promise a
    # non-preemptible floor while the renderer placed part of it on spot nodes.
    regular_pools = [pool for pool in known_pools if pool.capacity_type == "regular"]
    maximum_hot_floor = max(
        [spec.availability.min_replicas, *(window.min_replicas for window in spec.availability.warm_windows)]
    )
    regular_replica_capacity = sum(pool_replica_capacity[pool.pool_id] for pool in regular_pools)
    if regular_pools and maximum_hot_floor > regular_replica_capacity:
        issues.append(
            _issue(
                "regular_hot_capacity_infrastructure_required",
                "$.spec.availability",
                "hot and warm-window replica floors exceed the selected regular pool capacity",
                owner="terraform",
            )
        )
        terraform_inputs.update(f"accelerator_pools.{pool.pool_id}.max_nodes" for pool in regular_pools)

    requested_accelerators = spec.availability.max_replicas * spec.placement.accelerators_per_replica
    if requested_accelerators > envelope.max_accelerators_per_model:
        issues.append(
            _issue(
                "gpu_budget_exceeded",
                "$.spec.availability.maxReplicas",
                "requested accelerator ceiling exceeds the live model policy budget",
                owner="live-control-plane",
            )
        )
    if spec.queue.local_queue not in envelope.local_queues:
        issues.append(
            _issue(
                "queue_infrastructure_required",
                "$.spec.queue.localQueue",
                "queue is not present in the Terraform-owned infrastructure envelope",
                owner="terraform",
            )
        )
        terraform_inputs.add(f"queues.{spec.queue.local_queue}")
    if spec.queue.priority_class not in envelope.priority_classes:
        issues.append(
            _issue(
                "priority_class_infrastructure_required",
                "$.spec.queue.priorityClass",
                "priority class is not present in the Terraform-owned infrastructure envelope",
                owner="terraform",
            )
        )
        terraform_inputs.add(f"priority_classes.{spec.queue.priority_class}")
    if spec.tenant_id not in envelope.tenant_ids:
        issues.append(
            _issue(
                "tenant_outside_policy",
                "$.spec.tenantId",
                "tenant is outside the live control-plane policy envelope",
                owner="live-control-plane",
            )
        )

    live_errors = any(issue.owner == "live-control-plane" for issue in issues)
    infra_errors = any(issue.owner == "terraform" for issue in issues)
    disposition = (
        ValidationDisposition.REJECTED
        if live_errors
        else ValidationDisposition.INFRASTRUCTURE_REQUIRED
        if infra_errors
        else ValidationDisposition.ACCEPTED
    )
    admitted_pool_ref = None
    if disposition is ValidationDisposition.ACCEPTED and known_pools:
        # The compatibility field is the first pool used by the placement
        # planner. A regular pool is the deterministic home for the hot floor;
        # scale-to-zero models start with the largest preemptible segment.
        admitted_pool_ref = sorted(
            known_pools,
            key=lambda pool: (
                0
                if spec.availability.min_replicas > 0 and pool.capacity_type != "preemptible"
                else 1
                if spec.availability.min_replicas > 0
                else 0
                if pool.capacity_type == "preemptible"
                else 1,
                -pool_replica_capacity[pool.pool_id],
                pool.pool_id,
            ),
        )[0].pool_id
    return ValidationDecision(
        disposition=disposition,
        spec_digest=spec_digest(spec),
        issues=issues,
        terraform_inputs=sorted(terraform_inputs),
        admitted_pool_ref=admitted_pool_ref,
        fast_start=fast_start,
    )


class RenderContext(KubernetesModel):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    uid: str | None = Field(default=None, min_length=1, max_length=128)
    generation: int = Field(ge=1)
    pool: PoolEnvelope
    eligible_pools: list[PoolEnvelope] = Field(default_factory=list, max_length=32)
    prometheus_server_address: str = Field(min_length=1, max_length=2048)
    evaluation_time: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    minimum_total_replicas_override: int | None = Field(default=None, ge=0, le=10000)
    hot_floor_override: int | None = Field(default=None, ge=0, le=10000)
    preview: bool = False

    @model_validator(mode="after")
    def owner_required_for_apply(self) -> RenderContext:
        if not self.preview and self.uid is None:
            raise ValueError("a non-preview render requires the ModelDeployment UID")
        pools = self.eligible_pools or [self.pool]
        if len({item.pool_id for item in pools}) != len(pools):
            raise ValueError("render context eligible pools must be unique")
        if self.pool.pool_id not in {item.pool_id for item in pools}:
            raise ValueError("render context primary pool must be eligible")
        return self


class RenderedResource(KubernetesModel):
    api_version: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    manifest: dict[str, Any]
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    field_manager: Literal["fs2-model-controller"] = "fs2-model-controller"
    force_conflicts: Literal[False] = False


class RenderedServiceEndpoint(KubernetesModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    service_name: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    service_port: int = Field(ge=1, le=65535)

    @property
    def identity(self) -> str:
        return f"v1/Service/{self.namespace}/{self.service_name}"


class RenderPlan(KubernetesModel):
    renderer: str = Field(min_length=1, max_length=128)
    spec_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    render_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    resources: list[RenderedResource] = Field(min_length=1, max_length=256)
    endpoint: RenderedServiceEndpoint


class ModelRenderer(Protocol):
    name: str

    def render(self, spec: ModelDeploymentSpec, context: RenderContext) -> RenderPlan: ...


class LegacyTemplateBundle(KubernetesModel):
    model_ref: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    runtime_profile: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    template_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    primary_workload_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    runtime_container_name: str = Field(min_length=1, max_length=253, pattern=DNS_LABEL_PATTERN)
    primary_service_name: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    primary_service_port: int = Field(ge=1, le=65535)
    resources: list[dict[str, Any]] = Field(min_length=1, max_length=255)


_ALLOWED_TEMPLATE_GVKS = frozenset(
    {
        ("v1", "ConfigMap"),
        ("v1", "PersistentVolumeClaim"),
        ("v1", "Service"),
        ("v1", "ServiceAccount"),
        ("apps/v1", "Deployment"),
    }
)


def _resource_identity(manifest: Mapping[str, Any]) -> tuple[str, str, str, str]:
    metadata = manifest.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("template resource has no metadata object")
    api_version = manifest.get("apiVersion")
    kind = manifest.get("kind")
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if (
        not isinstance(api_version, str)
        or not api_version
        or not isinstance(kind, str)
        or not kind
        or not isinstance(namespace, str)
        or not namespace
        or not isinstance(name, str)
        or not name
    ):
        raise ValueError("template resource identity is incomplete")
    return api_version, kind, namespace, name


def _derived_name(prefix: str, name: str, *, maximum: int = 253) -> str:
    candidate = f"{prefix}{name}"
    if len(candidate) <= maximum:
        return candidate
    suffix = hashlib.sha256(candidate.encode("ascii")).hexdigest()[:12]
    stem = name[: maximum - len(prefix) - len(suffix) - 1].rstrip("-.")
    return f"{prefix}{stem}-{suffix}"


def bounded_label_value(value: str) -> str:
    """Return the stable Kubernetes label identity used for model ownership."""

    if len(value) <= 63:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    stem = value[:50].rstrip("-_.")
    return f"{stem}-{suffix}"


def _metric_name(model_ref: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_:]", "_", model_ref)
    return f"fs2_operation_demand_{suffix}"


def operation_demand_promql(model_ref: str) -> str:
    """Count every active state once, independent of gateway scrape replicas."""

    if re.fullmatch(MODEL_REF_PATTERN, model_ref) is None:
        raise ValueError("model reference cannot be embedded in PromQL")
    return (
        f'sum(max by (model, state) (fs2_serve_operations{{model="{model_ref}",'
        'state=~"queued|activating|running"})) OR vector(0)'
    )


def effective_hot_floor(spec: AvailabilitySpec, *, at: datetime) -> int:
    """Resolve recurring warm windows at one explicit reconciliation instant."""

    floor = spec.min_replicas
    for window in spec.warm_windows:
        local_now = at.astimezone(ZoneInfo(window.time_zone))
        # A one-microsecond offset includes a start that lands exactly on the
        # current instant while keeping croniter's previous-occurrence API.
        started_at = croniter(window.schedule, local_now + timedelta(microseconds=1)).get_prev(datetime)
        if started_at <= local_now < started_at + timedelta(seconds=window.duration_seconds):
            floor = max(floor, window.min_replicas)
    return min(floor, spec.max_replicas)


def scaled_object_name(model_deployment_name: str) -> str:
    """Return a stable ScaledObject name that leaves room for KEDA's HPA prefix."""

    return _derived_name("fs2-model-", model_deployment_name, maximum=253 - len("keda-hpa-"))


def scaled_object_hpa_name(model_deployment_name: str) -> str:
    return f"keda-hpa-{scaled_object_name(model_deployment_name)}"


@dataclass(frozen=True)
class _WorkloadSegment:
    """One exact-pool slice of the global replica interval."""

    pool: PoolEnvelope
    role: Literal["hot", "burst"]
    fixed_replicas: int | None
    minimum_replicas: int
    maximum_replicas: int
    demand_offset: int

    @property
    def autoscaled(self) -> bool:
        return self.fixed_replicas is None


def _pool_replica_capacity(pool: PoolEnvelope, accelerators_per_replica: int) -> int:
    return (pool.accelerators_per_node // accelerators_per_replica) * pool.max_nodes


def _ordered_hot_pools(pools: Sequence[PoolEnvelope], accelerators_per_replica: int) -> list[PoolEnvelope]:
    return sorted(
        pools,
        key=lambda pool: (
            pool.capacity_type == "preemptible",
            -_pool_replica_capacity(pool, accelerators_per_replica),
            pool.pool_id,
        ),
    )


def _ordered_burst_pools(pools: Sequence[PoolEnvelope], accelerators_per_replica: int) -> list[PoolEnvelope]:
    return sorted(
        pools,
        key=lambda pool: (
            pool.capacity_type != "preemptible",
            -_pool_replica_capacity(pool, accelerators_per_replica),
            pool.pool_id,
        ),
    )


def _workload_segments(
    spec: ModelDeploymentSpec,
    context: RenderContext,
    *,
    hot_floor: int,
) -> list[_WorkloadSegment]:
    """Partition the global replica range without duplicate scale ownership.

    A single-pool model retains the conventional one-Deployment KEDA shape.
    A heterogeneous policy gets fixed hot segments on regular capacity and
    independently autoscaled burst segments on preemptible capacity first.
    Every later segment observes a disjoint demand interval.
    """

    pools = context.eligible_pools or [context.pool]
    by_id = {pool.pool_id: pool for pool in pools}
    if set(by_id) != set(spec.placement.pool_refs):
        raise ValueError("render context pools differ from the admitted placement")
    if any(_pool_replica_capacity(pool, spec.placement.accelerators_per_replica) <= 0 for pool in pools):
        raise ValueError("an admitted pool has no usable replica capacity")

    if spec.lifecycle.desired_state is not DesiredState.ENABLED:
        return [
            _WorkloadSegment(
                pool=context.pool,
                role="hot",
                fixed_replicas=0,
                minimum_replicas=0,
                maximum_replicas=0,
                demand_offset=0,
            )
        ]

    if len(pools) == 1:
        pool = pools[0]
        if hot_floor == spec.availability.max_replicas:
            return [
                _WorkloadSegment(
                    pool=pool,
                    role="hot",
                    fixed_replicas=hot_floor,
                    minimum_replicas=hot_floor,
                    maximum_replicas=hot_floor,
                    demand_offset=0,
                )
            ]
        minimum = max(hot_floor, context.minimum_total_replicas_override or 0)
        return [
            _WorkloadSegment(
                pool=pool,
                role="burst",
                fixed_replicas=None,
                minimum_replicas=min(minimum, spec.availability.max_replicas),
                maximum_replicas=spec.availability.max_replicas,
                demand_offset=0,
            )
        ]

    pool_remaining = {
        pool.pool_id: _pool_replica_capacity(pool, spec.placement.accelerators_per_replica) for pool in pools
    }
    segments: list[_WorkloadSegment] = []
    floor_remaining = hot_floor
    regular_pools = [pool for pool in pools if pool.capacity_type == "regular"]
    hot_pools = regular_pools or list(pools)
    for pool in _ordered_hot_pools(hot_pools, spec.placement.accelerators_per_replica):
        allocated = min(floor_remaining, pool_remaining[pool.pool_id])
        if allocated > 0:
            segments.append(
                _WorkloadSegment(
                    pool=pool,
                    role="hot",
                    fixed_replicas=allocated,
                    minimum_replicas=allocated,
                    maximum_replicas=allocated,
                    demand_offset=0,
                )
            )
            floor_remaining -= allocated
            pool_remaining[pool.pool_id] -= allocated
        if floor_remaining == 0:
            break
    if floor_remaining:
        raise ValueError("hot replica floor exceeds admitted pool capacity")

    # Keep a stable zero-replica hot identity for a configured warm window so
    # entering the window scales an existing Deployment instead of replacing
    # the entire workload topology.
    if hot_floor == 0 and spec.availability.warm_windows:
        anchor = _ordered_hot_pools(pools, spec.placement.accelerators_per_replica)[0]
        segments.append(
            _WorkloadSegment(
                pool=anchor,
                role="hot",
                fixed_replicas=0,
                minimum_replicas=0,
                maximum_replicas=0,
                demand_offset=0,
            )
        )

    burst_remaining = spec.availability.max_replicas - hot_floor
    demand_offset = hot_floor
    requested_total_floor = max(hot_floor, context.minimum_total_replicas_override or 0)
    autoscaled_floor_remaining = min(
        requested_total_floor - hot_floor,
        burst_remaining,
    )
    for pool in _ordered_burst_pools(pools, spec.placement.accelerators_per_replica):
        capacity = min(burst_remaining, pool_remaining[pool.pool_id])
        if capacity <= 0:
            continue
        minimum = min(autoscaled_floor_remaining, capacity)
        segments.append(
            _WorkloadSegment(
                pool=pool,
                role="burst",
                fixed_replicas=None,
                minimum_replicas=minimum,
                maximum_replicas=capacity,
                demand_offset=demand_offset,
            )
        )
        demand_offset += capacity
        burst_remaining -= capacity
        autoscaled_floor_remaining -= minimum
        if burst_remaining == 0:
            break
    if burst_remaining:
        raise ValueError("global replica maximum exceeds admitted pool capacity")
    return segments


def _segmented_operation_demand_promql(
    model_ref: str,
    *,
    offset_replicas: int,
    capacity_replicas: int,
    target_queue_depth: int,
) -> str:
    base = operation_demand_promql(model_ref)
    if offset_replicas == 0:
        return base
    lower = offset_replicas * target_queue_depth
    upper = capacity_replicas * target_queue_depth
    return f"clamp_max(clamp_min(({base}) - {lower}, 0), {upper})"


class LegacyManifestRenderer:
    """Deterministically adapt a qualified existing manifest bundle.

    This renderer deliberately rejects Secrets, cluster-scoped objects, foreign
    namespaces, pre-existing owners/finalizers, and ambiguous primary
    workloads. It uses the selected pool's abstract resource name and scheduling
    contract, so the output is not tied to ``nvidia.com/gpu``.
    """

    name = "legacy-manifest-v1"

    def __init__(self, bundles: Mapping[tuple[str, str], LegacyTemplateBundle]) -> None:
        self._bundles = dict(bundles)

    def render(self, spec: ModelDeploymentSpec, context: RenderContext) -> RenderPlan:
        key = (spec.model_ref, spec.runtime.template_ref.digest)
        bundle = self._bundles.get(key)
        if bundle is None:
            raise ValueError("qualified legacy template bundle is unavailable")
        if (
            bundle.model_ref != spec.model_ref
            or bundle.runtime_profile != spec.runtime.profile
            or bundle.template_digest != spec.runtime.template_ref.digest
        ):
            raise ValueError("legacy template identity differs from desired state")

        labels = {
            "app.kubernetes.io/managed-by": "fs2-model-controller",
            "app.kubernetes.io/part-of": "fs2-serve",
            MODEL_DEPLOYMENT_LABEL: bounded_label_value(context.name),
            MODEL_ID_LABEL: bounded_label_value(spec.model_ref),
        }
        hot_floor = effective_hot_floor(spec.availability, at=context.evaluation_time)
        if context.hot_floor_override is not None:
            hot_floor = min(
                max(hot_floor, context.hot_floor_override),
                spec.availability.max_replicas,
            )
        annotations = {
            SPEC_DIGEST_ANNOTATION: spec_digest(spec),
            EFFECTIVE_HOT_FLOOR_ANNOTATION: str(hot_floor),
        }
        segments = _workload_segments(spec, context, hot_floor=hot_floor)
        multi_pool_layout = len(context.eligible_pools or [context.pool]) > 1
        owner_references: list[dict[str, Any]] = []
        if context.uid is not None:
            owner_references = [
                {
                    "apiVersion": API_VERSION,
                    "kind": KIND,
                    "name": context.name,
                    "uid": context.uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ]

        rendered: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        primary_found = False
        primary_service_found = False
        primary_template: dict[str, Any] | None = None
        primary_selector_labels: set[str] = set()
        for source in bundle.resources:
            manifest = copy.deepcopy(source)
            identity = _resource_identity(manifest)
            api_version, kind, namespace, name = identity
            if identity in seen:
                raise ValueError("legacy template contains duplicate resource identity")
            seen.add(identity)
            if (api_version, kind) not in _ALLOWED_TEMPLATE_GVKS:
                raise ValueError("legacy template contains an unsupported or cluster-scoped resource")
            if namespace != context.namespace:
                raise ValueError("legacy template resource is outside the ModelDeployment namespace")
            metadata = manifest["metadata"]
            forbidden_metadata = {
                "deletionTimestamp",
                "finalizers",
                "generateName",
                "managedFields",
                "ownerReferences",
                "resourceVersion",
                "uid",
            }
            if forbidden_metadata.intersection(metadata):
                raise ValueError("legacy template contains controller-owned metadata")
            metadata["labels"] = {**metadata.get("labels", {}), **labels}
            metadata["annotations"] = {**metadata.get("annotations", {}), **annotations}
            if owner_references:
                metadata["ownerReferences"] = owner_references
            manifest.pop("status", None)

            if kind == "Deployment" and name == bundle.primary_workload_name:
                if primary_found:
                    raise ValueError("legacy template has more than one primary workload")
                primary_found = True
                deployment_spec = manifest.get("spec")
                if not isinstance(deployment_spec, dict):
                    raise ValueError("primary Deployment spec is missing")
                deployment_spec["progressDeadlineSeconds"] = spec.rollout.progress_deadline_seconds
                deployment_spec["strategy"] = (
                    {
                        "type": "RollingUpdate",
                        "rollingUpdate": {
                            "maxUnavailable": spec.rollout.max_unavailable,
                            "maxSurge": spec.rollout.max_surge,
                        },
                    }
                    if spec.rollout.strategy is RolloutStrategy.ROLLING
                    else {"type": "Recreate"}
                )
                pod_spec = deployment_spec.get("template", {}).get("spec")
                if not isinstance(pod_spec, dict):
                    raise ValueError("primary Deployment Pod spec is missing")
                pod_metadata = deployment_spec.get("template", {}).get("metadata")
                if not isinstance(pod_metadata, dict):
                    raise ValueError("primary Deployment Pod metadata is missing")
                pod_metadata["labels"] = {**pod_metadata.get("labels", {}), **labels}
                pod_metadata["annotations"] = {**pod_metadata.get("annotations", {}), **annotations}
                containers = pod_spec.get("containers")
                if not isinstance(containers, list):
                    raise ValueError("primary Deployment containers are missing")
                matches = [
                    container for container in containers if container.get("name") == bundle.runtime_container_name
                ]
                if len(matches) != 1:
                    raise ValueError("runtime container identity is ambiguous")
                selector = deployment_spec.get("selector")
                if not isinstance(selector, Mapping):
                    raise ValueError("primary Deployment selector is missing")
                selector_labels = selector.get("matchLabels", {})
                if not isinstance(selector_labels, Mapping):
                    raise ValueError("primary Deployment matchLabels selector is invalid")
                primary_selector_labels = {str(value) for value in selector_labels}
                primary_template = manifest
                continue
            if kind == "Service" and name == bundle.primary_service_name:
                ports = manifest.get("spec", {}).get("ports", [])
                if (
                    not isinstance(ports, list)
                    or sum(
                        isinstance(port, Mapping) and port.get("port") == bundle.primary_service_port for port in ports
                    )
                    != 1
                ):
                    raise ValueError("primary Service does not expose the exact qualified port")
                if multi_pool_layout:
                    service_spec = manifest.get("spec")
                    if not isinstance(service_spec, dict):
                        raise ValueError("primary Service spec is missing")
                    service_spec["selector"] = {
                        MODEL_DEPLOYMENT_LABEL: bounded_label_value(context.name),
                    }
                primary_service_found = True
            rendered.append(manifest)

        if not primary_found:
            raise ValueError("legacy template primary workload is missing")
        if not primary_service_found:
            raise ValueError("legacy template primary Service is missing")
        if primary_template is None:
            raise ValueError("legacy template primary workload is missing")

        known_gpu_resources = {pool.resource_name for pool in (context.eligible_pools or [context.pool])}
        for segment in segments:
            workload = copy.deepcopy(primary_template)
            workload_metadata = workload["metadata"]
            deployment_spec = workload["spec"]
            pod_template = deployment_spec["template"]
            pod_metadata = pod_template["metadata"]
            pod_spec = pod_template["spec"]
            segment_identity = bounded_label_value(f"{segment.role}-{segment.pool.pool_id}")
            workload_name = (
                bundle.primary_workload_name
                if not multi_pool_layout
                else _derived_name(
                    "",
                    f"{bundle.primary_workload_name}-{segment.role}-{segment.pool.pool_id}",
                )
            )
            workload_metadata["name"] = workload_name
            workload_metadata["labels"] = {
                **workload_metadata.get("labels", {}),
                KUEUE_QUEUE_LABEL: spec.queue.local_queue,
                KUEUE_PRIORITY_LABEL: spec.queue.priority_class,
                WORKLOAD_ROLE_LABEL: segment_identity,
            }
            workload_metadata["annotations"] = {
                **workload_metadata.get("annotations", {}),
                WORKLOAD_POOL_ANNOTATION: segment.pool.pool_id,
                WORKLOAD_ROLE_ANNOTATION: segment.role,
                WORKLOAD_SEGMENT_CAPACITY_ANNOTATION: str(segment.maximum_replicas),
                WORKLOAD_SEGMENT_OFFSET_ANNOTATION: str(segment.demand_offset),
            }
            if multi_pool_layout:
                deployment_spec["selector"] = {
                    "matchLabels": {
                        MODEL_DEPLOYMENT_LABEL: bounded_label_value(context.name),
                        WORKLOAD_ROLE_LABEL: segment_identity,
                    }
                }
                template_labels = {
                    key: value
                    for key, value in pod_metadata.get("labels", {}).items()
                    if key not in primary_selector_labels
                }
                pod_metadata["labels"] = {
                    **template_labels,
                    **labels,
                    WORKLOAD_ROLE_LABEL: segment_identity,
                }
            pod_metadata["annotations"] = {
                **pod_metadata.get("annotations", {}),
                WORKLOAD_POOL_ANNOTATION: segment.pool.pool_id,
                WORKLOAD_ROLE_ANNOTATION: segment.role,
                WORKLOAD_SEGMENT_CAPACITY_ANNOTATION: str(segment.maximum_replicas),
                WORKLOAD_SEGMENT_OFFSET_ANNOTATION: str(segment.demand_offset),
            }
            pod_spec["nodeSelector"] = dict(segment.pool.node_selector)
            pod_spec["tolerations"] = copy.deepcopy(segment.pool.tolerations)
            affinity = pod_spec.get("affinity")
            if affinity is not None:
                if not isinstance(affinity, dict):
                    raise ValueError("primary Deployment affinity is invalid")
                affinity.pop("nodeAffinity", None)
                if not affinity:
                    pod_spec.pop("affinity", None)
            runtime_containers = [
                container
                for container in pod_spec["containers"]
                if container.get("name") == bundle.runtime_container_name
            ]
            container = runtime_containers[0]
            container["image"] = spec.runtime.image
            resources = container.setdefault("resources", {})
            for field in ("requests", "limits"):
                values = resources.setdefault(field, {})
                stale_gpu_names = [
                    resource_name
                    for resource_name in values
                    if resource_name != segment.pool.resource_name
                    and (resource_name in known_gpu_resources or resource_name.endswith("/gpu"))
                ]
                for stale in stale_gpu_names:
                    del values[stale]
                values[segment.pool.resource_name] = str(spec.placement.accelerators_per_replica)
            if segment.autoscaled:
                # KEDA owns only this segment's scale subresource. The model
                # controller owns every other field and never writes replicas
                # after the verified HPA handoff.
                deployment_spec.pop("replicas", None)
            else:
                deployment_spec["replicas"] = segment.fixed_replicas
            rendered.append(workload)

            if not segment.autoscaled:
                continue
            scaler_name = (
                scaled_object_name(context.name) if not multi_pool_layout else scaled_object_name(workload_name)
            )
            scaler_labels = {
                **labels,
                WORKLOAD_ROLE_LABEL: segment_identity,
            }
            scaler_annotations = {
                **annotations,
                WORKLOAD_POOL_ANNOTATION: segment.pool.pool_id,
                WORKLOAD_ROLE_ANNOTATION: segment.role,
                WORKLOAD_SEGMENT_CAPACITY_ANNOTATION: str(segment.maximum_replicas),
                WORKLOAD_SEGMENT_OFFSET_ANNOTATION: str(segment.demand_offset),
            }
            scaler: dict[str, Any] = {
                "apiVersion": "keda.sh/v1alpha1",
                "kind": "ScaledObject",
                "metadata": {
                    "name": scaler_name,
                    "namespace": context.namespace,
                    "labels": scaler_labels,
                    "annotations": scaler_annotations,
                },
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": workload_name,
                    },
                    "pollingInterval": spec.availability.polling_interval_seconds,
                    "cooldownPeriod": max(
                        spec.availability.idle_seconds,
                        spec.availability.cooldown_seconds,
                    ),
                    "minReplicaCount": segment.minimum_replicas,
                    "maxReplicaCount": segment.maximum_replicas,
                    "triggers": [
                        {
                            "type": "prometheus",
                            "metricType": "AverageValue",
                            "metadata": {
                                "serverAddress": context.prometheus_server_address,
                                "metricName": _metric_name(spec.model_ref),
                                "query": _segmented_operation_demand_promql(
                                    spec.model_ref,
                                    offset_replicas=segment.demand_offset,
                                    capacity_replicas=segment.maximum_replicas,
                                    target_queue_depth=spec.availability.target_queue_depth,
                                ),
                                "threshold": str(spec.availability.target_queue_depth),
                                "activationThreshold": "0",
                                "ignoreNullValues": "false",
                            },
                        }
                    ],
                },
            }
            if owner_references:
                scaler["metadata"]["ownerReferences"] = owner_references
            rendered.append(scaler)

        if spec.lifecycle.desired_state is DesiredState.ENABLED:
            if spec.exposure.open_ai or spec.exposure.mcp:
                publication: dict[str, Any] = {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": _derived_name("fs2-model-publication-", context.name),
                        "namespace": context.namespace,
                        "labels": {**labels, "fs2-serve.nebius.ai/component": "publication-intent"},
                        "annotations": annotations,
                    },
                    "data": {
                        "schema": "fs2-serve.nebius.ai/model-publication-intent/v1",
                        "model_id": spec.model_ref,
                        "tenant_id": spec.tenant_id,
                        "openai": str(spec.exposure.open_ai).lower(),
                        "openai_aliases_json": json.dumps(sorted(spec.exposure.open_ai_aliases), separators=(",", ":")),
                        "mcp": str(spec.exposure.mcp).lower(),
                        "mcp_tool_name": spec.exposure.mcp_tool_name or "",
                        "policy_ref": spec.policy.policy_ref,
                        "readiness_gate": "ModelDeployment/Ready=True",
                    },
                }
                if owner_references:
                    publication["metadata"]["ownerReferences"] = owner_references
                rendered.append(publication)

        if len(rendered) > 256:
            raise ValueError("rendered resource inventory exceeds the controller bound")

        resources = []
        for manifest in sorted(rendered, key=_resource_identity):
            api_version, kind, namespace, name = _resource_identity(manifest)
            resources.append(
                RenderedResource(
                    api_version=api_version,
                    kind=kind,
                    namespace=namespace,
                    name=name,
                    manifest=manifest,
                    digest=canonical_digest(manifest),
                )
            )
        resource_contract = [
            {
                "apiVersion": item.api_version,
                "kind": item.kind,
                "namespace": item.namespace,
                "name": item.name,
                "digest": item.digest,
            }
            for item in resources
        ]
        return RenderPlan(
            renderer=self.name,
            spec_digest=spec_digest(spec),
            render_digest=canonical_digest(resource_contract),
            resources=resources,
            endpoint=RenderedServiceEndpoint(
                namespace=context.namespace,
                service_name=bundle.primary_service_name,
                service_port=bundle.primary_service_port,
            ),
        )


class ReconcileAction(StrEnum):
    NOOP = "noop"
    OBSERVE = "observe"
    APPLY = "apply"
    DRAIN = "drain"
    DELETE = "delete"
    INFRASTRUCTURE_REQUIRED = "infrastructure-required"
    REJECT = "reject"
    RETRY = "retry"


class ObservedResource(KubernetesModel):
    api_version: str
    kind: str
    namespace: str
    name: str
    uid: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    controller_owner_uid: str | None = Field(default=None, min_length=1, max_length=128)
    field_managers: list[str] = Field(default_factory=list, max_length=64)
    deleting: bool = False

    @property
    def identity(self) -> str:
        return f"{self.api_version}/{self.kind}/{self.namespace}/{self.name}"


class AdoptionResourceEvidence(KubernetesModel):
    identity: str = Field(min_length=1, max_length=512)
    uid: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    field_managers: list[str] = Field(max_length=64)


class AdoptionVerification(KubernetesModel):
    receipt_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    terraform_state_released: bool
    inventory_complete: bool
    pre_diff_equal: bool
    conflicts_resolved: bool
    status_owned: bool = False
    resources: list[AdoptionResourceEvidence] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_resources(self) -> AdoptionVerification:
        if len({item.identity for item in self.resources}) != len(self.resources):
            raise ValueError("adoption evidence resource identities must be unique")
        return self


class DrainObservation(KubernetesModel):
    publication_withdrawn: bool
    active_operations: int | None = Field(default=None, ge=0)
    observed_replicas: int | None = Field(default=None, ge=0)
    ready_replicas: int | None = Field(default=None, ge=0)

    @property
    def complete(self) -> bool:
        return bool(
            self.publication_withdrawn
            and self.active_operations == 0
            and self.observed_replicas == 0
            and self.ready_replicas == 0
        )

    @property
    def preserve_runtime(self) -> bool:
        """Keep compute alive until active work is proven absent.

        Unknown operation evidence is fail-safe while a Deployment may still
        be running.  A positively observed zero-replica runtime is not brought
        back merely because Prometheus is temporarily unavailable.
        """

        if self.active_operations is not None:
            return self.active_operations > 0
        return (
            self.observed_replicas is None
            or self.ready_replicas is None
            or self.observed_replicas > 0
            or self.ready_replicas > 0
        )


class ReconcilePlan(KubernetesModel):
    action: ReconcileAction
    target_generation: int = Field(ge=1)
    spec_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    validation: ValidationDecision
    render: RenderPlan | None = None
    apply_resources: list[RenderedResource] = Field(default_factory=list, max_length=256)
    delete_resource_identities: list[str] = Field(default_factory=list, max_length=256)
    remove_finalizer: bool = False


def _rejected_decision(
    decision: ValidationDecision,
    *,
    code: str,
    path: str,
    message: str,
) -> ValidationDecision:
    return ValidationDecision(
        disposition=ValidationDisposition.REJECTED,
        spec_digest=decision.spec_digest,
        issues=[*decision.issues, _issue(code, path, message, owner="live-control-plane")],
        terraform_inputs=decision.terraform_inputs,
        fast_start=decision.fast_start,
    )


def _resource_map(resources: Sequence[ObservedResource]) -> dict[str, ObservedResource]:
    result: dict[str, ObservedResource] = {}
    for item in resources:
        if item.identity in result:
            raise ValueError("observed resource inventory contains duplicate identities")
        result[item.identity] = item
    return result


def _verify_adoption(
    spec: ModelDeploymentSpec,
    context: RenderContext,
    observed: Sequence[ObservedResource],
    verification: AdoptionVerification | None,
) -> str | None:
    receipt = spec.adoption.receipt_ref
    if receipt is None or verification is None:
        return "Claim adoption requires a verified immutable inventory receipt"
    if context.uid is None or verification.receipt_digest != receipt.digest:
        return "adoption verification is not bound to this ModelDeployment receipt"
    if not all(
        (
            verification.terraform_state_released,
            verification.inventory_complete,
            verification.pre_diff_equal,
            verification.conflicts_resolved,
        )
    ):
        return "adoption preconditions or Terraform ownership release are incomplete"
    actual = _resource_map(observed)
    expected = {item.identity: item for item in verification.resources}
    if set(actual) != set(expected):
        return "live adoption inventory differs from the reviewed receipt"
    for identity, evidence in expected.items():
        item = actual[identity]
        if verification.status_owned:
            if (
                item.uid != evidence.uid
                or item.controller_owner_uid != context.uid
                or FIELD_MANAGER not in item.field_managers
            ):
                return "owned resource UID, controller owner, or field manager is not recoverable"
        elif (
            item.uid != evidence.uid
            or item.digest != evidence.digest
            or sorted(item.field_managers) != sorted(evidence.field_managers)
            or item.controller_owner_uid not in {None, context.uid}
        ):
            return "live UID, digest, field managers, or owner differs from the adoption receipt"
    return None


def plan_reconciliation(
    *,
    generation: int,
    deleting: bool,
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope,
    renderer: ModelRenderer,
    render_context: RenderContext,
    observed: Sequence[ObservedResource],
    discovery_complete: bool,
    drain_observation: DrainObservation | None = None,
    adoption_verification: AdoptionVerification | None = None,
    current: ModelDeploymentSpec | None = None,
) -> ReconcilePlan:
    """Build one deterministic, side-effect-free reconcile action."""

    if render_context.preview or render_context.uid is None:
        raise ValueError("controller reconciliation requires a non-preview context with an exact CR UID")
    if generation != render_context.generation:
        raise ValueError("desired generation differs from the render context generation")
    validation = validate_model_deployment(
        spec,
        envelope,
        current=current,
        evaluation_time=render_context.evaluation_time,
    )
    if validation.disposition is ValidationDisposition.REJECTED:
        return ReconcilePlan(
            action=ReconcileAction.REJECT,
            target_generation=generation,
            spec_digest=validation.spec_digest,
            validation=validation,
        )
    if validation.disposition is ValidationDisposition.INFRASTRUCTURE_REQUIRED:
        return ReconcilePlan(
            action=ReconcileAction.INFRASTRUCTURE_REQUIRED,
            target_generation=generation,
            spec_digest=validation.spec_digest,
            validation=validation,
        )
    if validation.admitted_pool_ref != render_context.pool.pool_id:
        raise ValueError("render context pool differs from the deterministic admitted pool")
    context_pool_refs = {pool.pool_id for pool in (render_context.eligible_pools or [render_context.pool])}
    if context_pool_refs != set(spec.placement.pool_refs):
        raise ValueError("render context eligible pools differ from the admitted placement")

    if spec.adoption.mode is AdoptionMode.OBSERVE:
        return ReconcilePlan(
            action=ReconcileAction.OBSERVE,
            target_generation=generation,
            spec_digest=validation.spec_digest,
            validation=validation,
        )

    if not discovery_complete:
        return ReconcilePlan(
            action=ReconcileAction.RETRY,
            target_generation=generation,
            spec_digest=validation.spec_digest,
            validation=validation,
        )

    if spec.adoption.mode is AdoptionMode.CLAIM:
        problem = _verify_adoption(spec, render_context, observed, adoption_verification)
        if problem is not None:
            rejected = _rejected_decision(
                validation,
                code="adoption_verification_failed",
                path="$.spec.adoption",
                message=problem,
            )
            return ReconcilePlan(
                action=ReconcileAction.REJECT,
                target_generation=generation,
                spec_digest=rejected.spec_digest,
                validation=rejected,
            )

    effective_spec = spec
    effective_context = render_context
    drain_requested = deleting or spec.lifecycle.desired_state is not DesiredState.ENABLED
    if drain_requested:
        preserve_runtime = drain_observation is None or drain_observation.preserve_runtime
        if preserve_runtime:
            observed_floor = 1
            if drain_observation is not None and drain_observation.observed_replicas is not None:
                observed_floor = max(observed_floor, drain_observation.observed_replicas)
            effective_spec = spec.model_copy(
                update={
                    # Keep the scaler and runtime, but deliberately omit the
                    # publication intent so no new admissions can enter.
                    "lifecycle": LifecycleSpec(desired_state=DesiredState.ENABLED),
                    "exposure": spec.exposure.model_copy(
                        update={
                            "open_ai": False,
                            "open_ai_aliases": [],
                            "mcp": False,
                            "mcp_tool_name": None,
                        }
                    ),
                    "availability": spec.availability.model_copy(
                        update={"max_replicas": max(spec.availability.max_replicas, observed_floor)}
                    ),
                }
            )
            # Preserve the exact observed total without moving the hot/burst
            # boundary. This keeps active operations on their existing role
            # Deployment while the publication is withdrawn.
            effective_context = render_context.model_copy(update={"minimum_total_replicas_override": observed_floor})
        else:
            effective_spec = spec.model_copy(
                update={
                    "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
                    "availability": spec.availability.model_copy(update={"min_replicas": 0, "warm_windows": []}),
                }
            )

    render = renderer.render(effective_spec, effective_context)
    observed_by_identity = _resource_map(observed)
    desired_by_identity = {
        f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}": item for item in render.resources
    }

    if spec.adoption.mode is AdoptionMode.NONE:
        collisions = [
            identity
            for identity in desired_by_identity.keys() & observed_by_identity.keys()
            if observed_by_identity[identity].controller_owner_uid != render_context.uid
        ]
        if collisions:
            rejected = _rejected_decision(
                validation,
                code="foreign_resource_collision",
                path="$.metadata.name",
                message="a desired resource identity exists without this ModelDeployment controller owner",
            )
            return ReconcilePlan(
                action=ReconcileAction.REJECT,
                target_generation=generation,
                spec_digest=rejected.spec_digest,
                validation=rejected,
            )

    changes = [
        item
        for item in render.resources
        if (
            (seen := observed_by_identity.get(f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}")) is None
            or seen.digest != item.digest
        )
    ]
    stale_identities = sorted(
        identity
        for identity, item in observed_by_identity.items()
        if (
            item.controller_owner_uid == render_context.uid
            and not item.deleting
            and identity not in desired_by_identity
        )
    )

    if deleting:
        if drain_observation is None or not drain_observation.complete:
            return ReconcilePlan(
                action=ReconcileAction.DRAIN,
                target_generation=generation,
                spec_digest=validation.spec_digest,
                validation=validation,
                render=render,
                apply_resources=changes,
                delete_resource_identities=stale_identities,
            )
        owned = sorted(
            identity
            for identity, item in observed_by_identity.items()
            if item.controller_owner_uid == render_context.uid and not item.deleting
        )
        owned_remaining = any(item.controller_owner_uid == render_context.uid for item in observed_by_identity.values())
        return ReconcilePlan(
            action=ReconcileAction.DELETE if owned else ReconcileAction.NOOP,
            target_generation=generation,
            spec_digest=validation.spec_digest,
            validation=validation,
            delete_resource_identities=owned,
            remove_finalizer=not owned_remaining,
        )

    if spec.lifecycle.desired_state in {DesiredState.DRAINING, DesiredState.DISABLED}:
        action = ReconcileAction.DRAIN
    else:
        action = ReconcileAction.APPLY if changes or stale_identities else ReconcileAction.NOOP
    return ReconcilePlan(
        action=action,
        target_generation=generation,
        spec_digest=validation.spec_digest,
        validation=validation,
        render=render,
        apply_resources=changes,
        delete_resource_identities=stale_identities,
    )
