"""Typed declarative configuration contracts for the operator API.

The browser is deliberately not a Kubernetes or cloud control plane.  These
models describe desired state and reviewed handoffs; injected renderers and
reconcilers remain the only mutation boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import ModelId, StrictModel

SHA256_PATTERN = r"^[a-f0-9]{64}$"
DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
DNS_LABEL_PATTERN = r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$"
RESOURCE_NAME_PATTERN = r"^(?:[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$"


class ConfigurationOwner(StrEnum):
    RUNTIME = "runtime-reconciler"
    TERRAFORM = "terraform"


class ConfigurationPlanState(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ReconciliationPhase(StrEnum):
    PENDING = "pending"
    AWAITING_TERRAFORM = "awaiting-terraform-plan-apply"
    RENDERING = "rendering"
    APPLYING = "applying"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled-back"


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class SnapshotStrategy(StrEnum):
    DISABLED = "disabled"
    CUDA_CHECKPOINT = "cuda-checkpoint"
    RUNTIME_NATIVE = "runtime-native"
    WEIGHTS = "weights"


class CacheTier(StrEnum):
    OBJECT_STORE = "object-store"
    SHARED_FILESYSTEM = "shared-filesystem"
    NODE_LOCAL = "node-local"


class TolerationConfiguration(StrictModel):
    key: str = Field(min_length=1, max_length=253)
    operator: Literal["Equal", "Exists"] = "Equal"
    value: str | None = Field(default=None, max_length=253)
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None
    toleration_seconds: int | None = Field(default=None, ge=0, le=604800)

    @model_validator(mode="after")
    def validate_operator(self) -> TolerationConfiguration:
        if self.operator == "Exists" and self.value is not None:
            raise ValueError("Exists tolerations cannot set a value")
        if self.operator == "Equal" and self.value is None:
            raise ValueError("Equal tolerations require a value")
        if self.toleration_seconds is not None and self.effect != "NoExecute":
            raise ValueError("toleration_seconds is valid only for NoExecute")
        return self


class AcceleratorPoolConfiguration(StrictModel):
    resource_name: str = Field(min_length=1, max_length=253, pattern=RESOURCE_NAME_PATTERN)
    accelerator_class: str = Field(min_length=1, max_length=128)
    capacity_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$")
    accelerators_per_node: int = Field(ge=1, le=64)
    min_nodes: int = Field(ge=0, le=10000)
    max_nodes: int = Field(ge=0, le=10000)
    node_selector: dict[str, str] = Field(default_factory=dict, max_length=32)
    tolerations: list[TolerationConfiguration] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_bounds(self) -> AcceleratorPoolConfiguration:
        if self.max_nodes < self.min_nodes:
            raise ValueError("accelerator pool max_nodes must be greater than or equal to min_nodes")
        if len(set(self.node_selector)) != len(self.node_selector):
            raise ValueError("accelerator pool node selectors must be unique")
        return self


class PlacementConfiguration(StrictModel):
    pool_ids: list[str] = Field(min_length=1, max_length=32)
    accelerators: int = Field(ge=1, le=64)
    topology_policy: Literal["any", "single-node", "nvlink-domain"] = "any"

    @model_validator(mode="after")
    def validate_pool_ids(self) -> PlacementConfiguration:
        if len(self.pool_ids) != len(set(self.pool_ids)):
            raise ValueError("placement pool_ids must be unique")
        if any(not value or len(value) > 128 for value in self.pool_ids):
            raise ValueError("placement pool_id is outside the accepted bound")
        return self


class AutoscalingConfiguration(StrictModel):
    min_replicas: int = Field(ge=0, le=10000)
    max_replicas: int = Field(ge=0, le=10000)
    target_queue_depth: int = Field(default=1, ge=1, le=100000)
    polling_interval_seconds: int = Field(default=5, ge=1, le=60)
    cooldown_seconds: int = Field(default=300, ge=5, le=86400)

    @model_validator(mode="after")
    def validate_bounds(self) -> AutoscalingConfiguration:
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be greater than or equal to min_replicas")
        return self


class QueueConfiguration(StrictModel):
    local_queue: str = Field(min_length=1, max_length=253)
    priority_class: str = Field(min_length=1, max_length=253)
    max_queue_seconds: int = Field(default=7200, ge=1, le=604800)


class SnapshotConfiguration(StrictModel):
    strategy: SnapshotStrategy = SnapshotStrategy.DISABLED
    cache_tier: CacheTier = CacheTier.SHARED_FILESYSTEM
    restore_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    parallelism: int = Field(default=1, ge=1, le=128)
    require_semantic_check: bool = True


class McpConfiguration(StrictModel):
    exposed: bool = False
    tool_name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def validate_exposure(self) -> McpConfiguration:
        if self.exposed != (self.tool_name is not None):
            raise ValueError("MCP exposure and tool_name must be enabled or disabled together")
        return self


class RateConfiguration(StrictModel):
    requests_per_minute: int | None = Field(default=None, ge=1, le=1000000)
    concurrent_requests: int = Field(default=1, ge=1, le=10000)
    accelerator_seconds_per_day: int | None = Field(default=None, gt=0, le=1_000_000_000)


class ArtifactIdentity(StrictModel):
    image_repository: str = Field(min_length=1, max_length=512)
    image_digest: str = Field(pattern=DIGEST_PATTERN)
    model_revision: str = Field(min_length=1, max_length=256)
    artifact_manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    acquisition_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    provenance_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_health_contract_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_repository(self) -> ArtifactIdentity:
        if "@" in self.image_repository or self.image_repository.rsplit("/", 1)[-1].count(":"):
            raise ValueError("image_repository must not contain a tag or digest")
        return self


class ModelConfiguration(StrictModel):
    model_id: ModelId
    enabled: bool = True
    placement: PlacementConfiguration
    autoscaling: AutoscalingConfiguration
    queue: QueueConfiguration
    snapshot: SnapshotConfiguration = Field(default_factory=SnapshotConfiguration)
    mcp: McpConfiguration = Field(default_factory=McpConfiguration)
    rate: RateConfiguration = Field(default_factory=RateConfiguration)
    artifact: ArtifactIdentity


class PlatformConfiguration(StrictModel):
    schema_version: Literal["fs2.admin-configuration/v1"] = "fs2.admin-configuration/v1"
    pools: dict[str, AcceleratorPoolConfiguration] = Field(min_length=1, max_length=128)
    models: dict[ModelId, ModelConfiguration] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_references(self) -> PlatformConfiguration:
        pool_ids = set(self.pools)
        for pool_id in pool_ids:
            if not pool_id or len(pool_id) > 128:
                raise ValueError("accelerator pool identifier is outside the accepted bound")
        for model_id, model in self.models.items():
            if model_id != model.model_id:
                raise ValueError("model map key must equal model_id")
            missing = set(model.placement.pool_ids) - pool_ids
            if missing:
                raise ValueError(f"model {model_id} references unknown accelerator pools")
            if model.enabled and model.autoscaling.max_replicas == 0:
                raise ValueError(f"enabled model {model_id} must permit at least one replica")
        return self


class ConfigurationRevision(StrictModel):
    revision: int = Field(ge=1)
    etag: str = Field(pattern=SHA256_PATTERN)
    desired: PlatformConfiguration
    effective: PlatformConfiguration
    created_at: AwareDatetime
    created_by: str = Field(min_length=1, max_length=200)
    previous_revision: int | None = Field(default=None, ge=1)
    reconciliation_id: UUID | None = None


class ConfigurationProposal(StrictModel):
    base_etag: str = Field(pattern=SHA256_PATTERN)
    desired: PlatformConfiguration


class ConfigurationChange(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    owner: ConfigurationOwner
    before: Any = None
    after: Any = None


class ConfigurationDiff(StrictModel):
    base_revision: int = Field(ge=1)
    base_etag: str = Field(pattern=SHA256_PATTERN)
    proposed_etag: str = Field(pattern=SHA256_PATTERN)
    changes: list[ConfigurationChange] = Field(max_length=10000)
    runtime_change_count: int = Field(ge=0)
    terraform_change_count: int = Field(ge=0)


class ConfigurationValidationIssue(StrictModel):
    severity: ValidationSeverity
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    path: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=240)


class ConfigurationValidation(StrictModel):
    valid: bool
    proposed_etag: str = Field(pattern=SHA256_PATTERN)
    issues: list[ConfigurationValidationIssue] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_result(self) -> ConfigurationValidation:
        has_error = any(item.severity == ValidationSeverity.ERROR for item in self.issues)
        if self.valid == has_error:
            raise ValueError("validation valid flag disagrees with issues")
        return self


class RenderedConfigurationArtifact(StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=253)
    sha256: str = Field(pattern=SHA256_PATTERN)
    source: str = Field(min_length=1, max_length=512)


class TerraformHandoff(StrictModel):
    required: bool
    state: Literal["not-required", "review-required"]
    variables: dict[str, Any] = Field(max_length=128)
    variables_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_source_etag: str = Field(pattern=SHA256_PATTERN)
    tfvars_filename: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9][a-z0-9._-]*\.tfvars\.json$",
    )
    tfvars_json: str = Field(min_length=3, max_length=16 * 1024 * 1024)
    tfvars_sha256: str = Field(pattern=SHA256_PATTERN)
    forbidden_browser_actions: list[str] = Field(
        default_factory=lambda: ["terraform.apply", "cloud.mutate", "kubernetes.patch"]
    )

    @model_validator(mode="after")
    def validate_state(self) -> TerraformHandoff:
        if self.required != (self.state == "review-required"):
            raise ValueError("Terraform handoff state disagrees with required flag")
        return self


class TerraformApplyReceipt(StrictModel):
    schema_version: Literal["fs2.admin-terraform-apply/v1"] = "fs2.admin-terraform-apply/v1"
    plan_id: UUID
    reconciliation_id: UUID
    base_revision: int = Field(ge=1)
    base_etag: str = Field(pattern=SHA256_PATTERN)
    proposed_etag: str = Field(pattern=SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity(self) -> TerraformApplyReceipt:
        if self.plan_id != self.reconciliation_id:
            raise ValueError("Terraform apply receipt must use the plan-owned reconciliation identity")
        if self.proposed_etag != self.configuration_sha256:
            raise ValueError("Terraform apply receipt configuration identity differs from proposed state")
        if self.base_etag == self.proposed_etag:
            raise ValueError("Terraform apply receipt must describe a real change")
        return self


class ConfigurationPlan(StrictModel):
    plan_id: UUID
    state: ConfigurationPlanState
    base_revision: int = Field(ge=1)
    base_etag: str = Field(pattern=SHA256_PATTERN)
    proposed: PlatformConfiguration
    proposed_etag: str = Field(pattern=SHA256_PATTERN)
    validation: ConfigurationValidation
    diff: ConfigurationDiff
    artifacts: list[RenderedConfigurationArtifact] = Field(max_length=2048)
    terraform: TerraformHandoff
    created_at: AwareDatetime
    expires_at: AwareDatetime
    created_by: str = Field(min_length=1, max_length=200)


class ReconcileRequest(StrictModel):
    plan_id: UUID
    base_etag: str = Field(pattern=SHA256_PATTERN)


class ReconciliationStatus(StrictModel):
    reconciliation_id: UUID
    plan_id: UUID
    phase: ReconciliationPhase
    base_revision: int = Field(ge=1)
    target_etag: str = Field(pattern=SHA256_PATTERN)
    applied_revision: int | None = Field(default=None, ge=1)
    previous_revision: int | None = Field(default=None, ge=1)
    artifact_sha256: list[str] = Field(max_length=2048)
    terraform_variables_sha256: str = Field(pattern=SHA256_PATTERN)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


class RollbackRequest(StrictModel):
    target_revision: int = Field(ge=1)
    base_etag: str = Field(pattern=SHA256_PATTERN)


class RollbackPlan(StrictModel):
    target_revision: int = Field(ge=1)
    plan: ConfigurationPlan


class ConfigurationAuditReceipt(StrictModel):
    receipt_id: UUID
    action: Literal["validate", "plan", "reconcile", "rollback"]
    actor: str = Field(min_length=1, max_length=200)
    subject_sha256: str = Field(pattern=SHA256_PATTERN)
    occurred_at: AwareDatetime
