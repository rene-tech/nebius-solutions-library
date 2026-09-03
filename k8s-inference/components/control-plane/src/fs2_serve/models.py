"""Typed public and persistence models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

MAX_MODEL_ID_LENGTH = 128
MIN_IDEMPOTENCY_KEY_LENGTH = 8
MAX_IDEMPOTENCY_KEY_LENGTH = 200

ModelId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MODEL_ID_LENGTH)]
IdempotencyKey = Annotated[
    str,
    StringConstraints(min_length=MIN_IDEMPOTENCY_KEY_LENGTH, max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class KubernetesModel(StrictModel):
    """Strict model that accepts Kubernetes camelCase and Python snake_case."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Scope(StrEnum):
    CATALOG_READ = "catalog.read"
    INFERENCE_INVOKE = "inference.invoke"
    MCP_INVOKE = "mcp.invoke"
    OPERATIONS_READ = "operations.read"
    OPERATIONS_RESULT = "operations.result"
    ARTIFACTS_WRITE = "artifacts.write"
    OPERATIONS_CANCEL = "operations.cancel"
    OPERATIONS_ACKNOWLEDGE = "operations.acknowledge"
    TOKENS_MANAGE = "tokens.manage"
    AUDIT_READ = "audit.read"
    TENANT_ADMIN = "tenant.admin"
    USE_NONCLINICAL = "use.nonclinical"
    USE_NONCOMMERCIAL = "use.noncommercial"


class UsageDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class OperationStatus(StrEnum):
    QUEUED = "queued"
    ACTIVATING = "activating"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.PREEMPTED,
            self.EXPIRED,
        }


class ActivationAction(StrEnum):
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"


class ActivationIntentStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {self.READY, self.FAILED, self.EXPIRED}


class ActivationTargetState(StrictModel):
    model_id: ModelId
    target_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    resource_version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    observed_generation: int = Field(ge=1)
    template_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    active: bool
    observed_at: AwareDatetime
    controller_fencing_token: int = Field(ge=1)
    model_fencing_token: int = Field(default=1, ge=1)


class ActivationLeaderIdentity(StrictModel):
    """Exact Kubernetes identity and bounded authority observed after one Lease PATCH."""

    pod_namespace: str = Field(min_length=1, max_length=63)
    pod_name: str = Field(min_length=1, max_length=253)
    pod_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    service_account_name: str = Field(min_length=1, max_length=253)
    service_account_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    lease_namespace: str = Field(min_length=1, max_length=63)
    lease_name: str = Field(min_length=1, max_length=253)
    lease_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    lease_resource_version: str = Field(min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    lease_holder_identity: str = Field(min_length=1, max_length=253)
    lease_duration_seconds: int = Field(ge=5, le=60)
    lease_renew_time: AwareDatetime
    lease_observed_remaining_seconds: float = Field(gt=0, le=60)

    @property
    def controller_id(self) -> str:
        return f"{self.pod_namespace}/{self.pod_name}:{self.pod_uid}"

    def validate_binding(self) -> None:
        if self.lease_namespace != self.pod_namespace:
            raise ValueError("controller Pod and Lease namespaces differ")
        if self.lease_holder_identity != f"fs2:{self.pod_uid}":
            raise ValueError("Lease holder is not bound to the controller Pod UID")


class ActivationIntent(StrictModel):
    id: UUID
    operation_id: UUID | None = None
    operation_attempt: int = Field(ge=0, le=10)
    model_id: ModelId
    model_revision: str = Field(min_length=1, max_length=256)
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: ActivationAction
    status: ActivationIntentStatus
    requested_at: AwareDatetime
    available_at: AwareDatetime
    deadline_at: AwareDatetime | None = None
    attempt: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    controller_id: str | None = Field(default=None, max_length=200)
    fencing_token: int = Field(ge=0)
    model_fencing_token: int | None = Field(default=None, ge=1)
    leadership_fencing_token: int | None = Field(default=None, ge=1)
    lease_expires_at: AwareDatetime | None = None
    scale_contract_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target: ActivationTargetState | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


class ClaimedActivationIntent(ActivationIntent):
    controller_id: str = Field(min_length=1, max_length=200)
    lease_expires_at: AwareDatetime


class Principal(StrictModel):
    token_id: UUID
    token_prefix: str
    principal_id: str
    tenant_id: str
    scopes: frozenset[str]
    models: frozenset[ModelId]
    expires_at: AwareDatetime | None = None
    request_budget: int | None = None
    gpu_seconds_budget: float | None = None
    max_concurrency: int = 1

    def permits_model(self, model_id: str) -> bool:
        return "*" in self.models or model_id in self.models

    def require(self, scope: Scope | str, model_id: str | None = None) -> None:
        if str(scope) not in self.scopes:
            raise PermissionError(f"missing scope: {scope}")
        if model_id is not None and not self.permits_model(model_id):
            raise PermissionError("model is outside token policy")


class TokenCreate(StrictModel):
    principal_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    scopes: set[Scope] = Field(min_length=1)
    models: set[ModelId] = Field(min_length=1)
    expires_at: AwareDatetime | None = None
    request_budget: int | None = Field(default=None, ge=1)
    gpu_seconds_budget: float | None = Field(default=None, gt=0)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    rate_limit_requests: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_window_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_rate_window(self) -> TokenCreate:
        if (self.rate_limit_requests is None) != (self.rate_window_seconds is None):
            raise ValueError("rate limit and rate window must be configured together")
        return self


class TokenView(StrictModel):
    id: UUID
    prefix: str
    pepper_key_id: str
    principal_id: str
    tenant_id: str
    scopes: list[str]
    models: list[ModelId]
    expires_at: AwareDatetime | None
    request_budget: int | None
    requests_used: int
    gpu_seconds_budget: float | None
    gpu_seconds_used: float
    gpu_seconds_reserved: float
    max_concurrency: int
    created_at: AwareDatetime
    created_by: str
    revoked_at: AwareDatetime | None
    name: str | None = None
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    last_used_at: AwareDatetime | None = None
    rotation_parent_id: UUID | None = None
    rotated_at: AwareDatetime | None = None
    rate_limit_requests: int | None = Field(default=None, ge=1)
    rate_window_seconds: int | None = Field(default=None, ge=1)
    rate_window_started_at: AwareDatetime | None = None
    rate_window_requests: int = Field(default=0, ge=0)


class TokenIssued(TokenView):
    token: str


class AdmissionRequest(StrictModel):
    model_id: ModelId
    operation: str
    protocol: str
    idempotency_key: IdempotencyKey
    request_body: bytes
    request_content_type: str = "application/json"
    traceparent: str | None = Field(default=None, max_length=128)
    deadline_at: AwareDatetime | None = None


class PendingScientificAdmission(StrictModel):
    """One complete frozen batch admission committed with its Operation."""

    operation_id: UUID
    payload: dict[str, Any]
    created_at: AwareDatetime


class RuntimeIdentity(StrictModel):
    pod_uid: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    node_uid: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    gpu_count: int = Field(default=0, ge=0, le=64)
    preemptible: bool | None = None


class OperationView(StrictModel):
    id: UUID
    tenant_id: str
    principal_id: str
    token_id: UUID
    model_id: ModelId
    model_revision: str
    protocol: str
    operation: str
    idempotency_key: IdempotencyKey
    status: OperationStatus
    accepted_at: AwareDatetime
    available_at: AwareDatetime
    deadline_at: AwareDatetime | None = None
    activation_started_at: AwareDatetime | None = None
    ready_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    outcome: str | None = None
    semantic_outcome: str | None = None
    http_status: int | None = None
    response_content_type: str | None = None
    result_available: bool = False
    payload_expires_at: AwareDatetime | None = None
    error_code: str | None = None
    error_detail: str | None = None
    attempt: int = 0
    max_attempts: int = 1
    fencing_token: int = 0
    runtime: RuntimeIdentity = Field(default_factory=RuntimeIdentity)
    estimated_gpu_seconds: float = 0
    reserved_gpu_seconds: float = 0
    cold_start_seconds: float | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    modality_usage: list[ModalityUsage] = Field(default_factory=list, max_length=32)
    modality_usage_reported: bool = False
    reused: bool = False


class ClaimedOperation(OperationView):
    request_content_type: str
    traceparent: str | None = None
    dispatch_snapshot: str | None = Field(default=None, max_length=262_144)
    worker_id: str


class DynamicAdmissionFence(StrictModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    etag: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class OperationResult(StrictModel):
    operation: OperationView
    result: Any


class ModalityUsage(StrictModel):
    modality: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    direction: UsageDirection
    unit: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    amount: float = Field(ge=0)


class ReportedUsage(StrictModel):
    """Runtime-reported units only; the control plane never estimates these."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    modalities: list[ModalityUsage] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def unique_modalities(self) -> ReportedUsage:
        modalities = self.modalities or []
        identities = {(item.modality, item.direction, item.unit) for item in modalities}
        if len(identities) != len(modalities):
            raise ValueError("reported modality usage contains duplicate identities")
        if self.input_tokens is None and self.output_tokens is None and self.modalities is None:
            raise ValueError("reported usage contains no values")
        return self


class RuntimeResult(StrictModel):
    status_code: int
    body: bytes
    content_type: str
    elapsed_seconds: float = Field(ge=0)
    runtime: RuntimeIdentity
    semantic_outcome: str
    failure_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    usage: ReportedUsage | None = None


class AuditEvent(StrictModel):
    id: int
    occurred_at: AwareDatetime
    actor: str
    tenant_id: str | None
    token_id: UUID | None
    action: str
    target_type: str
    target_id: str
    outcome: str
    detail: dict[str, Any]


class TerminalAccounting(StrictModel):
    """Payload-free cumulative accounting projected from one fact per operation."""

    model_id: ModelId
    protocol: str
    outcome: str
    operations: int = Field(ge=0)
    estimated_gpu_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    cold_start_seconds: float = Field(ge=0)
