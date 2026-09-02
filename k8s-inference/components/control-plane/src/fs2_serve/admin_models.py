"""Typed, payload-free contracts for the read-only operator BFF."""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import ModelId, OperationStatus, StrictModel


class AdminSourceState(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class AdminValueState(StrEnum):
    AVAILABLE = "available"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class AdminModelState(StrEnum):
    HOT = "hot"
    LOADING = "loading"
    QUEUED = "queued"
    COLD = "cold"
    UNHEALTHY = "unhealthy"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class AdminActivationPhase(StrEnum):
    NONE = "none"
    QUEUED = "queued"
    CLAIMED = "claimed"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class AdminCapacityType(StrEnum):
    REGULAR = "regular"
    PREEMPTIBLE = "preemptible"
    UNKNOWN = "unknown"


class AdminWorkloadState(StrEnum):
    PENDING = "pending"
    ADMITTED = "admitted"
    FINISHED = "finished"
    UNKNOWN = "unknown"


class AdminCapabilityHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class AdminContextOption(StrictModel):
    project: str = Field(min_length=1, max_length=128)
    cluster: str = Field(min_length=1, max_length=128)
    region: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)


class AdminContext(StrictModel):
    project: str | None = None
    cluster: str | None = None
    region: str | None = None
    from_at: AwareDatetime
    to_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=64)


class AdminSource(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    state: AdminSourceState
    observed_at: AwareDatetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=200)


class AdminWarning(StrictModel):
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    message: str = Field(min_length=1, max_length=200)


class AdminMeta(StrictModel):
    schema_version: str = "fs2.admin-api/v1"
    generated_at: AwareDatetime
    context: AdminContext
    sources: list[AdminSource] = Field(max_length=16)
    warnings: list[AdminWarning] = Field(default_factory=list, max_length=16)


DataT = TypeVar("DataT")


class AdminEnvelope(StrictModel, Generic[DataT]):
    meta: AdminMeta
    data: DataT


class AdminProblem(StrictModel):
    type: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=100)
    status: int = Field(ge=400, le=599)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    detail: str = Field(min_length=1, max_length=200)
    request_id: UUID


class AdminContextData(StrictModel):
    selected: AdminContext
    options: list[AdminContextOption] = Field(max_length=128)
    server_authoritative: bool = True


class AdminMeasurement(StrictModel):
    value: float | None = None
    unit: str = Field(min_length=1, max_length=48)
    state: AdminValueState
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_availability(self) -> AdminMeasurement:
        if self.state == AdminValueState.UNAVAILABLE:
            if self.value is not None or self.reason is None:
                raise ValueError("unavailable admin value must be null with a reason")
        elif self.value is None:
            raise ValueError("available admin value must be numeric")
        elif self.state == AdminValueState.ESTIMATED and self.reason is None:
            raise ValueError("estimated admin value must explain the estimate")
        return self


class AdminLatency(StrictModel):
    p50_seconds: AdminMeasurement
    p95_seconds: AdminMeasurement
    p99_seconds: AdminMeasurement
    ttft_p95_seconds: AdminMeasurement


class AdminModelStateCount(StrictModel):
    state: AdminModelState
    models: int = Field(ge=0)


class AdminReconciliation(StrictModel):
    durable_terminal_operations: AdminMeasurement
    prometheus_terminal_operations: AdminMeasurement
    difference: AdminMeasurement


class AdminFleetCapacity(StrictModel):
    allocatable_gpus: AdminMeasurement
    ready_gpu_nodes: AdminMeasurement
    preemptible_gpu_nodes: AdminMeasurement
    active_gpu_replicas: AdminMeasurement


class AdminOverview(StrictModel):
    model_states: list[AdminModelStateCount]
    requests_per_second: AdminMeasurement
    tokens_per_second: AdminMeasurement
    terminal_operations: AdminMeasurement
    error_operations: AdminMeasurement
    error_rate: AdminMeasurement
    estimated_gpu_seconds: AdminMeasurement
    measured_gpu_seconds: AdminMeasurement
    queued_operations: AdminMeasurement
    oldest_queue_age_seconds: AdminMeasurement
    latency: AdminLatency
    capacity: AdminFleetCapacity
    reconciliation: AdminReconciliation


class AdminRuntimeOrigin(StrictModel):
    variant_id: str | None = Field(default=None, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    source_kind: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=1, max_length=512)
    relationship: str = Field(min_length=1, max_length=64)
    nim_artifact_parity: str = Field(min_length=1, max_length=64)


class AdminQualificationStates(StrictModel):
    registered: bool
    route_active: bool
    runtime_ready: bool
    semantic_qualified: bool
    http_mcp_qualified: bool
    cold_start_qualified: bool
    elasticity_qualified: bool


class AdminQualificationSnapshot(StrictModel):
    kind: str = Field(min_length=1, max_length=64)
    authority: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    states: AdminQualificationStates


class AdminModelPolicy(StrictModel):
    license_id: str = Field(min_length=1, max_length=256)
    non_clinical: bool
    commercial_use: str = Field(min_length=1, max_length=64)


class AdminModelIdentity(StrictModel):
    id: ModelId
    display_name: str = Field(min_length=1, max_length=200)
    family: str = Field(min_length=1, max_length=128)
    support_state: str = Field(min_length=1, max_length=64)
    enabled: bool
    model_revision: str | None = Field(default=None, max_length=256)
    runtime_kind: str = Field(min_length=1, max_length=128)
    runtime_image_digest: str | None = Field(default=None, max_length=256)
    gpu_class: str = Field(min_length=1, max_length=128)
    gpu_count: int = Field(ge=0, le=64)
    execution_mode: str = Field(min_length=1, max_length=64)
    protocols: list[str] = Field(max_length=32)
    public_endpoints: dict[str, str] = Field(max_length=32)
    mcp_exposed: bool
    mcp_tool_name: str | None = Field(default=None, max_length=128)
    active_runtime: AdminRuntimeOrigin | None = None
    qualification: AdminQualificationSnapshot | None = None
    policy: AdminModelPolicy


class AdminModelRuntime(StrictModel):
    state: AdminModelState
    reason: str = Field(min_length=1, max_length=200)
    activation_phase: AdminActivationPhase | None = None
    desired_replicas: int | None = Field(default=None, ge=0, le=10000)
    ready_replicas: int | None = Field(default=None, ge=0, le=10000)
    queued_operations: int | None = Field(default=None, ge=0)
    semantic_healthy: bool | None = None
    observed_at: AwareDatetime | None = None


class AdminModelMetrics(StrictModel):
    terminal_operations: AdminMeasurement
    requests_per_second: AdminMeasurement
    error_operations: AdminMeasurement
    error_rate: AdminMeasurement
    estimated_gpu_seconds: AdminMeasurement
    measured_gpu_seconds: AdminMeasurement
    tokens_per_second: AdminMeasurement
    latency: AdminLatency
    cold_start_seconds: AdminMeasurement


class AdminModelSummary(StrictModel):
    identity: AdminModelIdentity
    runtime: AdminModelRuntime
    metrics: AdminModelMetrics


class AdminModelList(StrictModel):
    items: list[AdminModelSummary] = Field(max_length=256)
    total: int = Field(ge=0, le=256)


class AdminModelDetail(StrictModel):
    model: AdminModelSummary
    snapshot_restore_seconds: AdminMeasurement
    cache_residency_bytes: AdminMeasurement
    cold_start_phase_breakdown: AdminMeasurement


class AdminOperationTiming(StrictModel):
    queue_seconds: AdminMeasurement
    cold_start_seconds: AdminMeasurement
    inference_seconds: AdminMeasurement
    total_seconds: AdminMeasurement
    ttft_seconds: AdminMeasurement


class AdminOperationItem(StrictModel):
    id: UUID
    tenant_id: str = Field(min_length=1, max_length=120)
    principal_id: str = Field(min_length=1, max_length=200)
    api_key_prefix: str = Field(min_length=1, max_length=64)
    model_id: ModelId
    model_revision: str = Field(min_length=1, max_length=256)
    protocol: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)
    status: OperationStatus
    accepted_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    outcome: str | None = Field(default=None, max_length=128)
    semantic_outcome: str | None = Field(default=None, max_length=128)
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_class: str | None = Field(default=None, max_length=64)
    attempt: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    gpu_count: int = Field(ge=0, le=64)
    preemptible: bool | None = None
    estimated_gpu_seconds: AdminMeasurement
    input_tokens: AdminMeasurement
    output_tokens: AdminMeasurement
    timings: AdminOperationTiming


class AdminOperationList(StrictModel):
    items: list[AdminOperationItem] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, max_length=512)


class AdminOperationDetail(StrictModel):
    operation: AdminOperationItem
    payloads_exposed: bool = False


# Store-side read models intentionally contain only fields approved for the BFF.
class AdminOperationRecord(StrictModel):
    id: UUID
    tenant_id: str
    principal_id: str
    api_key_prefix: str
    model_id: ModelId
    model_revision: str
    protocol: str
    operation: str
    status: OperationStatus
    accepted_at: AwareDatetime
    activation_started_at: AwareDatetime | None = None
    ready_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    outcome: str | None = None
    semantic_outcome: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    attempt: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    gpu_count: int = Field(ge=0, le=64)
    preemptible: bool | None = None
    estimated_gpu_seconds: float = Field(ge=0)
    cold_start_seconds: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AdminOperationQuery(StrictModel):
    from_at: AwareDatetime
    to_at: AwareDatetime
    limit: int = Field(ge=1, le=201)
    after_at: AwareDatetime | None = None
    after_id: UUID | None = None
    tenant_id: str | None = Field(default=None, min_length=1, max_length=120)
    model_id: ModelId | None = None
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    api_key_prefix: str | None = Field(default=None, min_length=1, max_length=64)
    status: OperationStatus | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def validate_bounds(self) -> AdminOperationQuery:
        if self.from_at >= self.to_at or (self.to_at - self.from_at).total_seconds() > 31 * 24 * 60 * 60:
            raise ValueError("admin operation window is outside the bound")
        if (self.after_at is None) != (self.after_id is None):
            raise ValueError("admin operation cursor is incomplete")
        return self


class AdminModelActivity(StrictModel):
    model_id: ModelId
    queued_operations: int = Field(ge=0)
    activation_phase: AdminActivationPhase
    observed_at: AwareDatetime


class AdminUsageRow(StrictModel):
    model_id: ModelId
    terminal_operations: int = Field(ge=0)
    error_operations: int = Field(ge=0)
    estimated_gpu_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    cold_start_seconds: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    token_reported_operations: int = Field(ge=0)
    latency_p50_seconds: float = Field(ge=0)
    latency_p95_seconds: float = Field(ge=0)
    latency_p99_seconds: float = Field(ge=0)


class AdminUsageWindow(StrictModel):
    from_at: AwareDatetime
    to_at: AwareDatetime
    rows: list[AdminUsageRow] = Field(max_length=256)
    latency_p50_seconds: float | None = Field(default=None, ge=0)
    latency_p95_seconds: float | None = Field(default=None, ge=0)
    latency_p99_seconds: float | None = Field(default=None, ge=0)


class AdminKubernetesModel(StrictModel):
    model_id: ModelId
    desired_replicas: int = Field(ge=0, le=10000)
    ready_replicas: int = Field(ge=0, le=10000)
    semantic_healthy: bool | None = None


class AdminKubernetesSnapshot(StrictModel):
    observed_at: AwareDatetime
    models: list[AdminKubernetesModel] = Field(max_length=256)
    allocatable_gpus: int = Field(ge=0)
    ready_gpu_nodes: int = Field(ge=0)
    preemptible_gpu_nodes: int = Field(ge=0)
    active_gpu_replicas: int = Field(ge=0)


class AdminPrometheusModel(StrictModel):
    model_id: ModelId
    requests_per_second: float | None = Field(default=None, ge=0)
    terminal_operations: int | None = Field(default=None, ge=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    latency_p50_seconds: float | None = Field(default=None, ge=0)
    latency_p95_seconds: float | None = Field(default=None, ge=0)
    latency_p99_seconds: float | None = Field(default=None, ge=0)


class AdminPrometheusSnapshot(StrictModel):
    observed_at: AwareDatetime
    models: list[AdminPrometheusModel] = Field(max_length=256)
    requests_per_second: float = Field(ge=0)
    terminal_operations: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    latency_p50_seconds: float | None = Field(default=None, ge=0)
    latency_p95_seconds: float | None = Field(default=None, ge=0)
    latency_p99_seconds: float | None = Field(default=None, ge=0)


class AdminQuantity(StrictModel):
    """A canonical Kubernetes quantity whose absence cannot be mistaken for zero."""

    value: str | None = Field(default=None, min_length=1, max_length=64)
    state: AdminValueState
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_availability(self) -> AdminQuantity:
        if self.state == AdminValueState.UNAVAILABLE:
            if self.value is not None or self.reason is None:
                raise ValueError("unavailable admin quantity must be null with a reason")
        elif self.value is None:
            raise ValueError("available admin quantity must have a canonical value")
        elif self.state == AdminValueState.ESTIMATED and self.reason is None:
            raise ValueError("estimated admin quantity must explain the estimate")
        return self


class AdminNodeCounts(StrictModel):
    total: AdminMeasurement
    ready: AdminMeasurement
    not_ready: AdminMeasurement
    unschedulable: AdminMeasurement


class AdminGpuResourceCapacity(StrictModel):
    resource_name: str = Field(min_length=1, max_length=128)
    capacity: AdminMeasurement
    allocatable: AdminMeasurement
    allocated: AdminMeasurement
    healthy: AdminMeasurement


class AdminNodePool(StrictModel):
    id: str = Field(pattern=r"^pool-[a-f0-9]{12}$")
    pool_label: str | None = Field(default=None, min_length=1, max_length=128)
    instance_type: str | None = Field(default=None, min_length=1, max_length=128)
    gpu_class: str | None = Field(default=None, min_length=1, max_length=128)
    capacity_type: AdminCapacityType
    nodes: AdminNodeCounts
    gpu_resources: list[AdminGpuResourceCapacity] = Field(max_length=32)


class AdminNodePoolInventory(StrictModel):
    state: AdminSourceState
    reason: str | None = Field(default=None, max_length=200)
    items: list[AdminNodePool] = Field(max_length=128)

    @model_validator(mode="after")
    def validate_collection_state(self) -> AdminNodePoolInventory:
        if self.state == AdminSourceState.AVAILABLE and self.reason is not None:
            raise ValueError("available node-pool inventory cannot have an unavailable reason")
        if self.state != AdminSourceState.AVAILABLE and (self.reason is None or self.items):
            raise ValueError("unavailable node-pool inventory must be empty with a reason")
        return self


class AdminKueueResourceQuota(StrictModel):
    flavor: str = Field(min_length=1, max_length=253)
    resource_name: str = Field(min_length=1, max_length=128)
    nominal_quota: AdminQuantity
    reservation: AdminQuantity
    usage: AdminQuantity
    borrowed: AdminQuantity


class AdminKueueWorkloadCounts(StrictModel):
    pending: AdminMeasurement
    reserving: AdminMeasurement
    admitted: AdminMeasurement


class AdminResourceFlavor(StrictModel):
    name: str = Field(min_length=1, max_length=253)
    capacity_type: AdminCapacityType
    gpu_class: str | None = Field(default=None, min_length=1, max_length=128)


class AdminClusterQueue(StrictModel):
    name: str = Field(min_length=1, max_length=253)
    cohort: str | None = Field(default=None, min_length=1, max_length=253)
    queueing_strategy: str | None = Field(default=None, min_length=1, max_length=64)
    stop_policy: str | None = Field(default=None, min_length=1, max_length=64)
    active: bool | None = None
    resources: list[AdminKueueResourceQuota] = Field(max_length=256)
    workloads: AdminKueueWorkloadCounts


class AdminLocalQueue(StrictModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    cluster_queue: str = Field(min_length=1, max_length=253)
    stop_policy: str | None = Field(default=None, min_length=1, max_length=64)
    active: bool | None = None
    workloads: AdminKueueWorkloadCounts


class AdminKueueCohort(StrictModel):
    name: str = Field(min_length=1, max_length=253)
    parent: str | None = Field(default=None, min_length=1, max_length=253)


class AdminKueueWorkload(StrictModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    local_queue: str | None = Field(default=None, min_length=1, max_length=253)
    cluster_queue: str | None = Field(default=None, min_length=1, max_length=253)
    state: AdminWorkloadState
    created_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, max_length=200)


class AdminKueueProjection(StrictModel):
    state: AdminSourceState
    reason: str | None = Field(default=None, max_length=200)
    resource_flavors: list[AdminResourceFlavor] = Field(max_length=128)
    cluster_queues: list[AdminClusterQueue] = Field(max_length=128)
    local_queues: list[AdminLocalQueue] = Field(max_length=256)
    cohorts: list[AdminKueueCohort] = Field(max_length=128)
    cohorts_state: AdminSourceState
    cohorts_reason: str | None = Field(default=None, max_length=200)
    workloads: list[AdminKueueWorkload] = Field(max_length=200)
    workloads_truncated: bool = False

    @model_validator(mode="after")
    def validate_collection_state(self) -> AdminKueueProjection:
        values = (self.resource_flavors, self.cluster_queues, self.local_queues, self.cohorts, self.workloads)
        if self.state == AdminSourceState.AVAILABLE and self.reason is not None:
            raise ValueError("available Kueue projection cannot have an unavailable reason")
        if self.state != AdminSourceState.AVAILABLE and (self.reason is None or any(values)):
            raise ValueError("unavailable Kueue projection must be empty with a reason")
        if self.cohorts_state == AdminSourceState.AVAILABLE and self.cohorts_reason is not None:
            raise ValueError("available Cohort projection cannot have an unavailable reason")
        if self.cohorts_state != AdminSourceState.AVAILABLE and (self.cohorts_reason is None or self.cohorts):
            raise ValueError("unavailable Cohort projection must be empty with a reason")
        return self


class AdminHorizontalAutoscaler(StrictModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    target_kind: str = Field(min_length=1, max_length=64)
    target_name: str = Field(min_length=1, max_length=253)
    min_replicas: AdminMeasurement
    max_replicas: AdminMeasurement
    current_replicas: AdminMeasurement
    desired_replicas: AdminMeasurement
    able_to_scale: bool | None = None
    scaling_active: bool | None = None
    scaling_limited: bool | None = None


class AdminKedaScaledObject(StrictModel):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=253)
    target_kind: str | None = Field(default=None, min_length=1, max_length=64)
    target_name: str = Field(min_length=1, max_length=253)
    min_replicas: AdminMeasurement
    max_replicas: AdminMeasurement
    ready: bool | None = None
    active: bool | None = None
    fallback: bool | None = None
    paused: bool | None = None


class AdminHorizontalAutoscalerInventory(StrictModel):
    state: AdminSourceState
    reason: str | None = Field(default=None, max_length=200)
    horizontal_pod_autoscalers: list[AdminHorizontalAutoscaler] = Field(max_length=256)

    @model_validator(mode="after")
    def validate_collection_state(self) -> AdminHorizontalAutoscalerInventory:
        if self.state == AdminSourceState.AVAILABLE and self.reason is not None:
            raise ValueError("available HPA projection cannot have an unavailable reason")
        if self.state != AdminSourceState.AVAILABLE and (self.reason is None or self.horizontal_pod_autoscalers):
            raise ValueError("unavailable HPA projection must be empty with a reason")
        return self


class AdminKedaScaledObjectInventory(StrictModel):
    state: AdminSourceState
    reason: str | None = Field(default=None, max_length=200)
    keda_scaled_objects: list[AdminKedaScaledObject] = Field(max_length=256)

    @model_validator(mode="after")
    def validate_collection_state(self) -> AdminKedaScaledObjectInventory:
        if self.state == AdminSourceState.AVAILABLE and self.reason is not None:
            raise ValueError("available KEDA projection cannot have an unavailable reason")
        if self.state != AdminSourceState.AVAILABLE and (self.reason is None or self.keda_scaled_objects):
            raise ValueError("unavailable KEDA projection must be empty with a reason")
        return self


class AdminAutoscalingProjection(StrictModel):
    hpa: AdminHorizontalAutoscalerInventory
    keda: AdminKedaScaledObjectInventory


class AdminNodeScalerProjection(StrictModel):
    state: AdminSourceState
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    configured: bool | None = None
    healthy: bool | None = None
    observed_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_state(self) -> AdminNodeScalerProjection:
        if self.state == AdminSourceState.AVAILABLE and (
            self.configured is not True
            or self.healthy is None
            or (self.healthy and self.reason is not None)
            or (not self.healthy and self.reason is None)
        ):
            raise ValueError("available node scaler must have a configured, explained health result")
        if self.state != AdminSourceState.AVAILABLE and self.reason is None:
            raise ValueError("unavailable node scaler must include a reason")
        return self


class AdminCapacity(StrictModel):
    node_pools: AdminNodePoolInventory
    kueue: AdminKueueProjection
    autoscaling: AdminAutoscalingProjection
    node_scaler: AdminNodeScalerProjection


class AdminCapacitySnapshot(StrictModel):
    observed_at: AwareDatetime
    data: AdminCapacity


class AdminObservabilityLaunch(StrictModel):
    enabled: bool
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_launch(self) -> AdminObservabilityLaunch:
        if self.enabled:
            if self.url is None or self.reason is not None or not self.url.startswith("https://"):
                raise ValueError("enabled observability launch requires a sanitized HTTPS URL")
        elif self.url is not None or self.reason is None:
            raise ValueError("disabled observability launch must omit URL and include a reason")
        return self


class AdminObservabilityComponent(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    installed: bool | None = None
    health: AdminCapabilityHealth
    data_present: bool | None = None
    launch: AdminObservabilityLaunch
    version: str | None = Field(default=None, min_length=1, max_length=64)
    observed_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, max_length=200)


class AdminObservabilitySignals(StrictModel):
    gpu_utilization_ratio: AdminMeasurement
    gpu_memory_utilization_ratio: AdminMeasurement
    otel_refused_items_per_second: AdminMeasurement
    otel_export_failures_per_second: AdminMeasurement


class AdminObservability(StrictModel):
    components: list[AdminObservabilityComponent] = Field(max_length=32)
    signals: AdminObservabilitySignals


class AdminObservabilitySnapshot(StrictModel):
    observed_at: AwareDatetime
    data: AdminObservability
