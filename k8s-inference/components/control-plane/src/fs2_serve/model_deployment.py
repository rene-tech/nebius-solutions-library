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
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from .models import StrictModel

API_VERSION = "inference.fs2.nebius.ai/v1alpha1"
KIND = "ModelDeployment"
FINALIZER = "inference.fs2.nebius.ai/model-cleanup"
FIELD_MANAGER = "fs2-model-controller"
SPEC_DIGEST_ANNOTATION = "fs2-serve.nebius.ai/spec-digest"
MODEL_DEPLOYMENT_LABEL = "fs2-serve.nebius.ai/model-deployment"
MODEL_ID_LABEL = "fs2-serve.nebius.ai/model-id"

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
DNS_LABEL_PATTERN = r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"
DNS_SUBDOMAIN_PATTERN = (
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$"
)
MODEL_REF_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$"
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


class KubernetesModel(StrictModel):
    """Strict model that accepts Kubernetes camelCase and Python snake_case."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        alias_generator=to_camel,
        populate_by_name=True,
    )


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
    capacity_type: str = Field(min_length=1, max_length=64)
    accelerators_per_node: int = Field(ge=1, le=64)
    min_nodes: int = Field(ge=0, le=10000)
    max_nodes: int = Field(ge=0, le=10000)
    node_selector: dict[str, str] = Field(default_factory=dict, max_length=32)
    tolerations: list[dict[str, str | int]] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_capacity(self) -> PoolEnvelope:
        if self.max_nodes < self.min_nodes:
            raise ValueError("pool maxNodes must be greater than or equal to minNodes")
        return self


class ModelQualification(KubernetesModel):
    model_ref: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    runtime_profile: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    artifact_manifest_digests: list[str] = Field(min_length=1, max_length=64)
    runtime_images: list[str] = Field(min_length=1, max_length=64)
    accelerator_classes: list[str] = Field(min_length=1, max_length=128)
    max_accelerators_per_replica: int = Field(ge=1, le=64)
    template_digests: list[str] = Field(min_length=1, max_length=64)
    snapshot_digests: list[str] = Field(default_factory=list, max_length=64)


class InfrastructureEnvelope(KubernetesModel):
    revision: str = Field(pattern=SHA256_DIGEST_PATTERN)
    pools: dict[str, PoolEnvelope] = Field(min_length=1, max_length=128)
    qualifications: dict[str, ModelQualification] = Field(min_length=1, max_length=512)
    local_queues: list[str] = Field(min_length=1, max_length=128)
    priority_classes: list[str] = Field(min_length=1, max_length=128)
    tenant_ids: list[str] = Field(min_length=1, max_length=1024)
    max_accelerators_per_model: int = Field(default=1024, ge=1, le=640000)

    @model_validator(mode="after")
    def key_identities_match(self) -> InfrastructureEnvelope:
        if any(key != pool.pool_id for key, pool in self.pools.items()):
            raise ValueError("pool map key must match poolId")
        if any(key != item.model_ref for key, item in self.qualifications.items()):
            raise ValueError("qualification map key must match modelRef")
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
) -> ValidationDecision:
    """Validate one revision before any renderer, Kubernetes, or cloud action."""

    issues: list[ValidationIssue] = []
    terraform_inputs: set[str] = set()

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
        if spec.artifact.manifest_digest not in qualification.artifact_manifest_digests:
            issues.append(
                _issue(
                    "artifact_digest_unqualified",
                    "$.spec.artifact.manifestDigest",
                    "artifact manifest digest is not qualified for this model",
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
        if spec.runtime.template_ref.digest not in qualification.template_digests:
            issues.append(
                _issue(
                    "template_digest_unqualified",
                    "$.spec.runtime.templateRef.digest",
                    "runtime template digest is not qualified for this model",
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
    possible_replicas = max(pool_replica_capacity.values(), default=0)
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
        admitted_pool_ref = sorted(
            known_pools,
            key=lambda pool: (-pool_replica_capacity[pool.pool_id], pool.pool_id),
        )[0].pool_id
    return ValidationDecision(
        disposition=disposition,
        spec_digest=spec_digest(spec),
        issues=issues,
        terraform_inputs=sorted(terraform_inputs),
        admitted_pool_ref=admitted_pool_ref,
    )


class RenderContext(KubernetesModel):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_PATTERN)
    uid: str | None = Field(default=None, min_length=1, max_length=128)
    generation: int = Field(ge=1)
    pool: PoolEnvelope
    prometheus_server_address: str = Field(min_length=1, max_length=2048)
    preview: bool = False

    @model_validator(mode="after")
    def owner_required_for_apply(self) -> RenderContext:
        if not self.preview and self.uid is None:
            raise ValueError("a non-preview render requires the ModelDeployment UID")
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


class RenderPlan(KubernetesModel):
    renderer: str = Field(min_length=1, max_length=128)
    spec_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    render_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    resources: list[RenderedResource] = Field(min_length=1, max_length=256)


class ModelRenderer(Protocol):
    name: str

    def render(self, spec: ModelDeploymentSpec, context: RenderContext) -> RenderPlan: ...


class LegacyTemplateBundle(KubernetesModel):
    model_ref: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    runtime_profile: str = Field(min_length=1, max_length=128, pattern=MODEL_REF_PATTERN)
    template_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    primary_workload_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    runtime_container_name: str = Field(min_length=1, max_length=253, pattern=DNS_LABEL_PATTERN)
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


def _label_value(value: str) -> str:
    if len(value) <= 63:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    stem = value[:50].rstrip("-_.")
    return f"{stem}-{suffix}"


def _metric_name(model_ref: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_:]", "_", model_ref)
    return f"fs2_operation_demand_{suffix}"


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
            MODEL_DEPLOYMENT_LABEL: _label_value(context.name),
            MODEL_ID_LABEL: _label_value(spec.model_ref),
        }
        annotations = {SPEC_DIGEST_ANNOTATION: spec_digest(spec)}
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
                deployment_spec["replicas"] = 0
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
                pod_spec["nodeSelector"] = dict(context.pool.node_selector)
                pod_spec["tolerations"] = copy.deepcopy(context.pool.tolerations)
                containers = pod_spec.get("containers")
                if not isinstance(containers, list):
                    raise ValueError("primary Deployment containers are missing")
                matches = [
                    container
                    for container in containers
                    if container.get("name") == bundle.runtime_container_name
                ]
                if len(matches) != 1:
                    raise ValueError("runtime container identity is ambiguous")
                container = matches[0]
                container["image"] = spec.runtime.image
                resources = container.setdefault("resources", {})
                for field in ("requests", "limits"):
                    values = resources.setdefault(field, {})
                    stale_gpu_names = [
                        key
                        for key in values
                        if key.endswith("/gpu") and key != context.pool.resource_name
                    ]
                    for stale in stale_gpu_names:
                        del values[stale]
                    values[context.pool.resource_name] = str(spec.placement.accelerators_per_replica)
            rendered.append(manifest)

        if not primary_found:
            raise ValueError("legacy template primary workload is missing")

        if spec.lifecycle.desired_state is DesiredState.ENABLED:
            scaler: dict[str, Any] = {
                "apiVersion": "keda.sh/v1alpha1",
                "kind": "ScaledObject",
                "metadata": {
                    "name": _derived_name("fs2-model-", context.name),
                    "namespace": context.namespace,
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": bundle.primary_workload_name,
                    },
                    "pollingInterval": spec.availability.polling_interval_seconds,
                    "cooldownPeriod": spec.availability.cooldown_seconds,
                    "minReplicaCount": spec.availability.min_replicas,
                    "maxReplicaCount": spec.availability.max_replicas,
                    "fallback": {
                        "failureThreshold": 3,
                        "replicas": max(1, spec.availability.min_replicas),
                        "behavior": "static",
                    },
                    "triggers": [
                        {
                            "type": "prometheus",
                            "metricType": "AverageValue",
                            "metadata": {
                                "serverAddress": context.prometheus_server_address,
                                "metricName": _metric_name(spec.model_ref),
                                "query": (
                                    f'max(fs2_serve_operations{{model="{spec.model_ref}",'
                                    'state=~"queued|activating|running"}) OR vector(0)'
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
                        "openai_aliases_json": json.dumps(
                            sorted(spec.exposure.open_ai_aliases), separators=(",", ":")
                        ),
                        "mcp": str(spec.exposure.mcp).lower(),
                        "mcp_tool_name": spec.exposure.mcp_tool_name or "",
                        "policy_ref": spec.policy.policy_ref,
                        "readiness_gate": "ModelDeployment/Ready=True",
                    },
                }
                if owner_references:
                    publication["metadata"]["ownerReferences"] = owner_references
                rendered.append(publication)

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
    validation = validate_model_deployment(spec, envelope, current=current)
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
    if deleting and spec.lifecycle.desired_state is DesiredState.ENABLED:
        effective_spec = spec.model_copy(
            update={
                "lifecycle": LifecycleSpec(desired_state=DesiredState.DRAINING),
                "availability": spec.availability.model_copy(
                    update={"min_replicas": 0, "warm_windows": []}
                ),
            }
        )

    render = renderer.render(effective_spec, render_context)
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
            (
                seen := observed_by_identity.get(
                    f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}"
                )
            )
            is None
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
        owned_remaining = any(
            item.controller_owner_uid == render_context.uid for item in observed_by_identity.values()
        )
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
