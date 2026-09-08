"""App identity and payload-free admin contracts.

An app names one independently managed execution target; model_ref names its
qualified model source. Existing operations are joined, never imported again.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .admin_models import AdminOperationItem
from .model_deployment import ModelDeploymentSpec
from .model_deployment_records import ModelDeploymentRevision
from .models import StrictModel
from .request_telemetry import RequestTelemetry, RequestTransportUsage
from .scientific_admin_models import ScientificModelPolicy, ScientificModelPolicyUpdate, ScientificRunDetail


class AppRecord(StrictModel):
    app_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    model_ref: str = Field(min_length=1, max_length=128)
    public_model_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["serving", "scientific"]
    namespace: str
    deployment_name: str | None = None
    academic_required: bool = False
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AppCapabilities(StrictModel):
    duplicate: bool
    reusable_workers: bool
    live_settings: bool


class AppSummary(AppRecord):
    enabled: bool
    status: str
    status_reason: str | None = None
    capabilities: AppCapabilities
    logical_run_count: int | None = None
    last_used_at: AwareDatetime | None = None


class AppList(StrictModel):
    items: list[AppSummary]
    next_cursor: str | None = None


class AppCreate(StrictModel):
    model_ref: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    source_app_id: UUID | None = None


class AppChoice(StrictModel):
    app_id: str
    display_name: str
    public_model_id: str
    academic_required: bool


class AppObservabilityTarget(StrictModel):
    app_id: str
    model_id: str
    namespace: str
    pod_labels: dict[str, str]
    deployment_name: str | None = None
    execution_mode: Literal["serving", "scientific"]


class AppRun(StrictModel):
    app_id: UUID
    operation: AdminOperationItem
    scientific: ScientificRunDetail | None = None
    observed_transport: list[RequestTelemetry] = Field(default_factory=list)


class AppRunList(StrictModel):
    items: list[AppRun]
    next_cursor: str | None = None


class AppSettings(StrictModel):
    app_id: UUID
    execution_mode: Literal["serving", "scientific"]
    app_revision: int
    display_name: str
    academic_required: bool
    serving: ModelDeploymentRevision | None = None
    scientific: ScientificModelPolicy | None = None
    capabilities: AppCapabilities
    unsupported_reason: str | None = None


class AppSettingsUpdate(StrictModel):
    expected_app_revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    academic_required: bool | None = None
    serving_spec: ModelDeploymentSpec | None = None
    serving_base_etag: str | None = None
    scientific_policy: ScientificModelPolicyUpdate | None = None


class AppScientificUsage(StrictModel):
    occupied_seconds: float
    active_compute_seconds: float
    occupied_idle_seconds: float


class AppUserUsage(StrictModel):
    user_id: str
    tenant_id: str
    principal_id: str
    logical_runs: int


class AppUsageBucket(StrictModel):
    timestamp: AwareDatetime
    logical_runs: int


class AppUsage(StrictModel):
    app_id: UUID
    logical_runs: int
    succeeded_runs: int
    failed_runs: int
    active_runs: int
    first_used_at: AwareDatetime | None = None
    last_used_at: AwareDatetime | None = None
    estimated_gpu_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    scientific_gpu: AppScientificUsage | None = None
    unique_users: int = 0
    users: list[AppUserUsage] = Field(default_factory=list)
    status_classes: dict[str, int] = Field(default_factory=dict)
    requests_over_time: list[AppUsageBucket] = Field(default_factory=list)
    time_bucket_seconds: int = 3600
    request_bytes: int | None = None
    response_bytes: int | None = None
    observed_transport: RequestTransportUsage | None = None
    notes: list[str] = Field(default_factory=list)
