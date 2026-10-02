"""Payload-free customer/workbench contracts; never return cloud specifications."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from .models import StrictModel


class CustomerProfile(StrictModel):
    display_name: str = Field(min_length=1, max_length=160)
    purpose: Literal["customer", "internal", "system", "demo", "speech", "legacy"] = "customer"
    archived: bool = False


class WorkbenchRegistration(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    principal_ids: list[str] = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    management: Literal["managed", "customer"] = "managed"
    endpoint_id: str = Field(pattern=r"^aiendpoint-[a-z0-9]+$", max_length=120)
    project_id: str = Field(pattern=r"^project-[a-z0-9]+$", max_length=120)
    protected: bool = False
    protection_reason: str = Field(default="", max_length=300)

    @field_validator("principal_ids")
    @classmethod
    def unique_members(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 160 for item in value):
            raise ValueError("invalid inference principal")
        return sorted(set(value))


class WorkbenchBinding(WorkbenchRegistration):
    id: UUID
    bucket_name: str | None = None
    state_filesystem_id: str | None = None
    desired_release: str | None = None
    revision: int = 1
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EndpointObservation(StrictModel):
    resource_id: str
    project_id: str
    kind: Literal["endpoint"] = "endpoint"
    name: str
    state: str
    image: str
    platform: str
    preset: str
    url: str | None = None
    buckets: list[str] = Field(default_factory=list)
    is_workbench: bool = False
    state_filesystem_id: str | None = None
    persistent_state: bool = False

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query:
                raise ValueError("workbench URL must be HTTPS without credentials or query parameters")
        return value


class BucketObservation(StrictModel):
    resource_id: str
    project_id: str
    kind: Literal["bucket"] = "bucket"
    name: str
    size_bytes: int | None = None
    quota_bytes: int | None = None
    purpose: str | None = None
    managed_by: str | None = None


class WorkbenchCommand(StrictModel):
    kind: Literal["backup", "upgrade", "restore", "retire"]
    idempotency_key: str = Field(min_length=8, max_length=120)
    expected_revision: int = Field(ge=1)
    target_release: str | None = Field(default=None, max_length=300)
    backup_id: UUID | None = None
    confirm_interruption: bool = False


class WorkbenchOperation(StrictModel):
    id: UUID
    workbench_id: UUID
    kind: str
    state: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error_code: str | None = None
    progress: dict[str, object] = Field(default_factory=dict)
