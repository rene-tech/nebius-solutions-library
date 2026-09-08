"""Inference owners are independent of console operators and individual keys."""

from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field

from .access_models import AdminApiKey, PrincipalKind
from .admin_models import AdminMeasurement
from .models import StrictModel


def owner_id(tenant_id: str, principal_id: str) -> UUID:
    """A stable identity for legacy owners, without writing during a read."""
    return uuid5(NAMESPACE_URL, f"fs2:inference-owner:{tenant_id}:{principal_id}")


class UserSettings(StrictModel):
    display_name: str = Field(min_length=1, max_length=160)
    kind: PrincipalKind | None = None
    team: str | None = Field(default=None, max_length=160)
    enabled: bool = True
    # Null preserves the existing key policy; it is not an academic assertion.
    academic_eligible: bool | None = None
    app_ids: list[UUID] | None = Field(default=None, max_length=1000)


class UserCreate(UserSettings):
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    principal_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    kind: PrincipalKind


class UserPatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    kind: PrincipalKind | None = None
    team: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    academic_eligible: bool | None = None
    app_ids: list[UUID] | None = Field(default=None, max_length=1000)


class InferenceUser(UserSettings):
    id: UUID
    tenant_id: str
    principal_id: str
    source: Literal["configured", "existing-key-owner"]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UserUsagePoint(StrictModel):
    at: AwareDatetime
    requests: int = Field(ge=0)


class UserUsage(StrictModel):
    """Logical operations accepted in [from,to), never polls or key rotations."""

    requests: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    scientific_requests: int = Field(ge=0)
    last_request_at: AwareDatetime | None = None
    scheduler_occupied_gpu_seconds: AdminMeasurement
    active_gpu_seconds: AdminMeasurement
    occupied_idle_gpu_seconds: AdminMeasurement
    input_tokens: AdminMeasurement
    output_tokens: AdminMeasurement
    request_series: list[UserUsagePoint] = Field(default_factory=list)
    bucket_seconds: int = Field(default=60, ge=1)
    attribution: str = "All keys for this tenant and inference owner; accepted-in-window cohort."


class UserRow(InferenceUser):
    key_count: int = Field(ge=0)
    active_key_count: int = Field(ge=0)
    usage: UserUsage


class UserList(StrictModel):
    items: list[UserRow]
    limit: int
    truncated: bool = False
    apps: list[UserAppChoice] = Field(default_factory=list)


class UserAppChoice(StrictModel):
    app_id: UUID
    display_name: str
    public_model_id: str
    academic_required: bool


class UserDetail(StrictModel):
    user: UserRow
    keys: list[AdminApiKey]
    apps: list[UserAppChoice]
    policy_note: str = (
        "User settings restrict, never expand, each API key's policy. Existing key limits remain per key. "
        "Disabling stops new invocation, while existing operations and results remain readable."
    )
