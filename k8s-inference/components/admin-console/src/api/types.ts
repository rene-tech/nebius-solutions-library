export type SourceState = "available" | "stale" | "unavailable";
export type ValueState = "available" | "estimated" | "unavailable";
export type ModelState =
  | "hot"
  | "loading"
  | "queued"
  | "cold"
  | "unhealthy"
  | "unsupported"
  | "unknown";

export type OperationState =
  | "queued"
  | "activating"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "preempted"
  | "expired";

export interface AdminContext {
  project: string | null;
  cluster: string | null;
  region: string | null;
  from_at: string;
  to_at: string;
  timezone: string;
}

export interface AdminSource {
  id: string;
  state: SourceState;
  observed_at: string | null;
  age_seconds: number | null;
  reason: string | null;
}

export interface AdminWarning {
  source: string;
  code: string;
  message: string;
}

export interface AdminMeta {
  schema_version: "fs2.admin-api/v1";
  generated_at: string;
  context: AdminContext;
  sources: AdminSource[];
  warnings: AdminWarning[];
}

export interface AdminEnvelope<T> {
  meta: AdminMeta;
  data: T;
}

export interface AdminContextOption {
  project: string;
  cluster: string;
  region: string;
  label: string;
}

export interface AdminContextData {
  selected: AdminContext;
  options: AdminContextOption[];
  server_authoritative: true;
}

export interface AdminMeasurement {
  value: number | null;
  unit: string;
  state: ValueState;
  source: string;
  reason: string | null;
}

export interface AdminLatency {
  p50_seconds: AdminMeasurement;
  p95_seconds: AdminMeasurement;
  p99_seconds: AdminMeasurement;
  ttft_p95_seconds: AdminMeasurement;
}

export interface AdminOverview {
  model_states: Array<{ state: ModelState; models: number }>;
  requests_per_second: AdminMeasurement;
  tokens_per_second: AdminMeasurement;
  terminal_operations: AdminMeasurement;
  error_operations: AdminMeasurement;
  error_rate: AdminMeasurement;
  estimated_gpu_seconds: AdminMeasurement;
  measured_gpu_seconds: AdminMeasurement;
  queued_operations: AdminMeasurement;
  oldest_queue_age_seconds: AdminMeasurement;
  latency: AdminLatency;
  capacity: {
    allocatable_gpus: AdminMeasurement;
    ready_gpu_nodes: AdminMeasurement;
    preemptible_gpu_nodes: AdminMeasurement;
    active_gpu_replicas: AdminMeasurement;
  };
  reconciliation: {
    durable_terminal_operations: AdminMeasurement;
    prometheus_terminal_operations: AdminMeasurement;
    difference: AdminMeasurement;
  };
}

export interface AdminModelIdentity {
  id: string;
  display_name: string;
  family: string;
  support_state: string;
  enabled: boolean;
  model_revision: string | null;
  runtime_kind: string;
  runtime_image_digest: string | null;
  gpu_class: string;
  gpu_count: number;
  execution_mode: string;
  protocols: string[];
  public_endpoints: Record<string, string>;
  mcp_exposed: boolean;
  mcp_tool_name: string | null;
  active_runtime: {
    variant_id: string | null;
    kind: string;
    source_kind: string;
    repository: string;
    relationship: string;
    nim_artifact_parity: string;
  } | null;
  qualification: {
    kind: "reviewed-evidence-snapshot";
    authority: string;
    observed_at: string;
    states: {
      registered: boolean;
      route_active: boolean;
      runtime_ready: boolean;
      semantic_qualified: boolean;
      http_mcp_qualified: boolean;
      cold_start_qualified: boolean;
      elasticity_qualified: boolean;
    };
  } | null;
  policy: {
    license_id: string;
    non_clinical: boolean;
    commercial_use: string;
  };
}

export interface AdminModelRuntime {
  state: ModelState;
  reason: string;
  activation_phase: string | null;
  desired_replicas: number | null;
  ready_replicas: number | null;
  queued_operations: number | null;
  semantic_healthy: boolean | null;
  observed_at: string | null;
}

export interface AdminModelMetrics {
  terminal_operations: AdminMeasurement;
  requests_per_second: AdminMeasurement;
  error_operations: AdminMeasurement;
  error_rate: AdminMeasurement;
  estimated_gpu_seconds: AdminMeasurement;
  measured_gpu_seconds: AdminMeasurement;
  tokens_per_second: AdminMeasurement;
  latency: AdminLatency;
  cold_start_seconds: AdminMeasurement;
}

export interface AdminModelSummary {
  identity: AdminModelIdentity;
  runtime: AdminModelRuntime;
  metrics: AdminModelMetrics;
}

export interface AdminModelList {
  items: AdminModelSummary[];
  total: number;
}

export interface AdminModelDetail {
  model: AdminModelSummary;
  snapshot_restore_seconds: AdminMeasurement;
  cache_residency_bytes: AdminMeasurement;
  cold_start_phase_breakdown: AdminMeasurement;
}

export interface AdminOperationItem {
  id: string;
  tenant_id: string;
  principal_id: string;
  api_key_prefix: string;
  model_id: string;
  model_revision: string;
  protocol: string;
  operation: string;
  status: OperationState;
  accepted_at: string;
  completed_at: string | null;
  outcome: string | null;
  semantic_outcome: string | null;
  http_status: number | null;
  error_class: string | null;
  attempt: number;
  max_attempts: number;
  gpu_count: number;
  preemptible: boolean | null;
  estimated_gpu_seconds: AdminMeasurement;
  input_tokens: AdminMeasurement;
  output_tokens: AdminMeasurement;
  timings: {
    queue_seconds: AdminMeasurement;
    cold_start_seconds: AdminMeasurement;
    inference_seconds: AdminMeasurement;
    total_seconds: AdminMeasurement;
    ttft_seconds: AdminMeasurement;
  };
}

export interface AdminOperationList {
  items: AdminOperationItem[];
  next_cursor: string | null;
}

export interface AdminOperationDetail {
  operation: AdminOperationItem;
  payloads_exposed: false;
}

export type AdminCapacityType = "regular" | "preemptible" | "unknown";
export type AdminWorkloadState = "pending" | "admitted" | "finished" | "unknown";
export type AdminCapabilityHealth = "healthy" | "degraded" | "unhealthy" | "unknown";

export interface AdminQuantity {
  value: string | null;
  state: ValueState;
  source: string;
  reason: string | null;
}

export interface AdminNodeCounts {
  total: AdminMeasurement;
  ready: AdminMeasurement;
  not_ready: AdminMeasurement;
  unschedulable: AdminMeasurement;
}

export interface AdminGpuResourceCapacity {
  resource_name: string;
  capacity: AdminMeasurement;
  allocatable: AdminMeasurement;
  allocated: AdminMeasurement;
  healthy: AdminMeasurement;
}

export interface AdminNodePool {
  id: string;
  pool_label: string | null;
  instance_type: string | null;
  gpu_class: string | null;
  capacity_type: AdminCapacityType;
  nodes: AdminNodeCounts;
  gpu_resources: AdminGpuResourceCapacity[];
}

export interface AdminNodePoolInventory {
  state: SourceState;
  reason: string | null;
  items: AdminNodePool[];
}

export interface AdminKueueResourceQuota {
  flavor: string;
  resource_name: string;
  nominal_quota: AdminQuantity;
  reservation: AdminQuantity;
  usage: AdminQuantity;
  borrowed: AdminQuantity;
}

export interface AdminKueueWorkloadCounts {
  pending: AdminMeasurement;
  reserving: AdminMeasurement;
  admitted: AdminMeasurement;
}

export interface AdminResourceFlavor {
  name: string;
  capacity_type: AdminCapacityType;
  gpu_class: string | null;
}

export interface AdminClusterQueue {
  name: string;
  cohort: string | null;
  queueing_strategy: string | null;
  stop_policy: string | null;
  active: boolean | null;
  resources: AdminKueueResourceQuota[];
  workloads: AdminKueueWorkloadCounts;
}

export interface AdminLocalQueue {
  namespace: string;
  name: string;
  cluster_queue: string;
  stop_policy: string | null;
  active: boolean | null;
  workloads: AdminKueueWorkloadCounts;
}

export interface AdminKueueCohort {
  name: string;
  parent: string | null;
}

export interface AdminKueueWorkload {
  namespace: string;
  name: string;
  local_queue: string | null;
  cluster_queue: string | null;
  state: AdminWorkloadState;
  created_at: string | null;
  reason: string | null;
}

export interface AdminKueueProjection {
  state: SourceState;
  reason: string | null;
  resource_flavors: AdminResourceFlavor[];
  cluster_queues: AdminClusterQueue[];
  local_queues: AdminLocalQueue[];
  cohorts: AdminKueueCohort[];
  cohorts_state: SourceState;
  cohorts_reason: string | null;
  workloads: AdminKueueWorkload[];
  workloads_truncated: boolean;
}

export interface AdminHorizontalAutoscaler {
  namespace: string;
  name: string;
  target_kind: string;
  target_name: string;
  min_replicas: AdminMeasurement;
  max_replicas: AdminMeasurement;
  current_replicas: AdminMeasurement;
  desired_replicas: AdminMeasurement;
  able_to_scale: boolean | null;
  scaling_active: boolean | null;
  scaling_limited: boolean | null;
}

export interface AdminKedaScaledObject {
  namespace: string;
  name: string;
  target_kind: string | null;
  target_name: string;
  min_replicas: AdminMeasurement;
  max_replicas: AdminMeasurement;
  ready: boolean | null;
  active: boolean | null;
  fallback: boolean | null;
  paused: boolean | null;
}

export interface AdminCapacity {
  node_pools: AdminNodePoolInventory;
  kueue: AdminKueueProjection;
  autoscaling: {
    hpa: {
      state: SourceState;
      reason: string | null;
      horizontal_pod_autoscalers: AdminHorizontalAutoscaler[];
    };
    keda: {
      state: SourceState;
      reason: string | null;
      keda_scaled_objects: AdminKedaScaledObject[];
    };
  };
  node_scaler: {
    state: SourceState;
    provider: string | null;
    configured: boolean | null;
    healthy: boolean | null;
    observed_at: string | null;
    reason: string | null;
  };
}

export interface AdminObservabilityLaunch {
  enabled: boolean;
  url: string | null;
  reason: string | null;
}

export interface AdminObservabilityComponent {
  id: string;
  display_name: string;
  installed: boolean | null;
  health: AdminCapabilityHealth;
  data_present: boolean | null;
  launch: AdminObservabilityLaunch;
  version: string | null;
  observed_at: string | null;
  reason: string | null;
}

export interface AdminObservability {
  components: AdminObservabilityComponent[];
  signals: {
    gpu_utilization_ratio: AdminMeasurement;
    gpu_memory_utilization_ratio: AdminMeasurement;
    otel_refused_items_per_second: AdminMeasurement;
    otel_export_failures_per_second: AdminMeasurement;
  };
}
