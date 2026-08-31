"""Typed, secret-free contracts for operator access and browser sessions."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import AuditEvent, ModalityUsage, ModelId, Scope, StrictModel, TokenCreate

BOOTSTRAP_OPERATOR_PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000001")


class OperatorRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    def permits(self, required: OperatorRole) -> bool:
        order = {
            OperatorRole.VIEWER: 0,
            OperatorRole.OPERATOR: 1,
            OperatorRole.ADMIN: 2,
        }
        return order[self] >= order[required]


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"


class ApiKeyState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ROTATED = "rotated"


class AccessValueState(StrEnum):
    AVAILABLE = "available"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class OperatorPrincipalCreate(StrictModel):
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    kind: PrincipalKind
    role: OperatorRole
    tenant_id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class OperatorPrincipalPatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: OperatorRole | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> OperatorPrincipalPatch:
        if self.display_name is None and self.role is None and self.enabled is None:
            raise ValueError("principal update contains no changes")
        return self


class OperatorPrincipal(StrictModel):
    id: UUID
    subject: str
    display_name: str
    kind: PrincipalKind
    role: OperatorRole
    tenant_id: str | None = None
    enabled: bool
    created_at: AwareDatetime
    created_by: str
    updated_at: AwareDatetime
    disabled_at: AwareDatetime | None = None

    @property
    def global_access(self) -> bool:
        return self.tenant_id is None

    def require(self, required: OperatorRole, *, tenant_id: str | None = None) -> None:
        if not self.enabled or not self.role.permits(required):
            raise PermissionError("operator role is insufficient")
        if tenant_id is not None and self.tenant_id is not None and tenant_id != self.tenant_id:
            raise PermissionError("tenant is outside operator policy")


class OperatorSession(StrictModel):
    id: UUID
    principal: OperatorPrincipal
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_seen_at: AwareDatetime
    revoked_at: AwareDatetime | None = None


class OperatorSessionRecord(StrictModel):
    """Internal persistence projection; never use as an HTTP response model."""

    session: OperatorSession
    pepper_key_id: str = Field(min_length=1, max_length=64)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class OperatorSessionHandoff(StrictModel):
    """Bootstrap-authenticated selection of the server-side operator identity."""

    principal_id: UUID = BOOTSTRAP_OPERATOR_PRINCIPAL_ID


class AdminApiKeyCreate(TokenCreate):
    name: str = Field(min_length=1, max_length=120)


class AdminApiKeyRotate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    expires_at: AwareDatetime | None = None


class AdminApiKeyPolicyPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    scopes: set[Scope] | None = Field(default=None, min_length=1)
    models: set[ModelId] | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = None
    request_budget: int | None = Field(default=None, ge=1)
    gpu_seconds_budget: float | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    rate_limit_requests: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_window_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_patch(self) -> AdminApiKeyPolicyPatch:
        fields = self.model_fields_set
        if not fields:
            raise ValueError("API-key policy update contains no changes")
        if any(name in fields and getattr(self, name) is None for name in ("scopes", "models", "max_concurrency")):
            raise ValueError("scopes, models, and concurrency cannot be cleared")
        if ("rate_limit_requests" in fields) != ("rate_window_seconds" in fields):
            raise ValueError("rate limit and rate window must be updated together")
        if "rate_limit_requests" in fields and (
            (self.rate_limit_requests is None) != (self.rate_window_seconds is None)
        ):
            raise ValueError("rate limit and rate window must both be values or both be null")
        return self


class AccessMeasurement(StrictModel):
    value: float | None = None
    unit: str = Field(min_length=1, max_length=32)
    state: AccessValueState
    reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_state(self) -> AccessMeasurement:
        if self.state is AccessValueState.UNAVAILABLE:
            if self.value is not None or self.reason is None:
                raise ValueError("unavailable access value must be null with a reason")
        elif self.value is None:
            raise ValueError("available access value must be numeric")
        elif self.state is AccessValueState.ESTIMATED and self.reason is None:
            raise ValueError("estimated access value requires a reason")
        return self


class AdminApiKeyUsage(StrictModel):
    terminal_operations: int = Field(ge=0)
    estimated_gpu_seconds: AccessMeasurement
    input_tokens: AccessMeasurement
    output_tokens: AccessMeasurement
    token_reported_operations: int = Field(ge=0)
    modality_reported_operations: int = Field(ge=0)
    modality_units: list[ModalityUsage] = Field(default_factory=list, max_length=32)
    modality_state: AccessValueState
    modality_reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_modality_state(self) -> AdminApiKeyUsage:
        if self.modality_state is AccessValueState.UNAVAILABLE and self.modality_reason is None:
            raise ValueError("unavailable modality usage requires a reason")
        if self.modality_state is AccessValueState.UNAVAILABLE and self.modality_units:
            raise ValueError("unavailable modality usage cannot expose incomplete totals")
        return self


class AdminApiKey(StrictModel):
    id: UUID
    name: str | None = None
    prefix: str
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    principal_id: str
    tenant_id: str
    scopes: list[str]
    models: list[str]
    state: ApiKeyState
    expires_at: AwareDatetime | None = None
    last_used_at: AwareDatetime | None = None
    request_budget: int | None = Field(default=None, ge=1)
    requests_used: int = Field(ge=0)
    gpu_seconds_budget: float | None = Field(default=None, gt=0)
    gpu_seconds_used: float = Field(ge=0)
    gpu_seconds_reserved: float = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    rate_limit_requests: int | None = Field(default=None, ge=1)
    rate_window_seconds: int | None = Field(default=None, ge=1)
    rate_window_started_at: AwareDatetime | None = None
    rate_window_requests: int = Field(ge=0)
    rotation_parent_id: UUID | None = None
    rotated_at: AwareDatetime | None = None
    created_at: AwareDatetime
    created_by: str
    revoked_at: AwareDatetime | None = None
    usage: AdminApiKeyUsage


class AdminApiKeyDisclosure(StrictModel):
    key: AdminApiKey
    secret: str = Field(min_length=32, max_length=256)


class AdminApiKeyList(StrictModel):
    items: list[AdminApiKey] = Field(max_length=1000)


class AdminPrincipalList(StrictModel):
    items: list[OperatorPrincipal] = Field(max_length=1000)


class AdminAuditList(StrictModel):
    items: list[AuditEvent] = Field(max_length=1000)


class AdminKeyUsageRecord(StrictModel):
    token_id: UUID
    terminal_operations: int = Field(ge=0)
    estimated_gpu_seconds: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_reported_operations: int = Field(ge=0)
    modality_reported_operations: int = Field(ge=0)
    modality_units: list[ModalityUsage] = Field(default_factory=list, max_length=32)
