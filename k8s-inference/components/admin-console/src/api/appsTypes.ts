import type { TimeSeriesChartProps } from "../components/TimeSeriesChart";
import type { AdminOperationItem } from "./types";
import type {
  ScientificModelPolicy,
  ScientificModelPolicyUpdate,
  ScientificRunDetail,
} from "./scientificTypes";
import type {
  ModelDeploymentRevision,
  ModelDeploymentSpec,
} from "./modelDeploymentTypes";

export interface AppSummary {
  app_id: string;
  display_name: string;
  model_ref: string;
  public_model_id: string;
  execution_mode: "serving" | "scientific";
  namespace: string;
  deployment_name: string | null;
  academic_required: boolean;
  enabled: boolean;
  status: string;
  status_reason: string | null;
  created_at: string;
  updated_at: string;
  capabilities: {
    duplicate: boolean;
    reusable_workers: boolean;
    live_settings: boolean;
  };
  logical_run_count: number | null;
  last_used_at: string | null;
}
export interface AppList {
  items: AppSummary[];
  next_cursor: string | null;
}
export interface AppCreate {
  model_ref: string;
  display_name: string;
  source_app_id?: string;
}
export interface ObservedTransport {
  request_id: string;
  started_at: string;
  completed_at: string;
  endpoint: string;
  method: string;
  transport: "http" | "mcp";
  http_status: number | null;
  response_duration_seconds: number;
  request_bytes: number | null;
  response_bytes: number | null;
  request_bytes_observed: number;
  response_bytes_observed: number;
  request_complete: boolean;
  response_complete: boolean;
  disconnected: boolean;
  error_type: string | null;
  tenant_id: string | null;
  principal_id: string | null;
  token_id: string | null;
  model_id: string | null;
  operation_id: string | null;
  mcp_tool: string | null;
  mcp_is_error: boolean | null;
}
export interface ObservedTransportUsage {
  request_count: number;
  completed_response_count: number;
  incomplete_response_count: number;
  successful_http_count: number;
  failed_http_count: number;
  mcp_tool_error_count: number;
  request_bytes: number | null;
  response_bytes: number | null;
  request_bytes_known_count: number;
  response_bytes_known_count: number;
  average_response_duration_seconds: number | null;
  maximum_response_duration_seconds: number | null;
  first_observed_at: string | null;
  last_observed_at: string | null;
  status_classes: Record<string, number>;
  requests_over_time: Array<{
    timestamp: string;
    request_count: number | null;
    status_classes: Record<string, number> | null;
  }>;
  time_bucket_seconds: number;
}
export interface AppRun {
  app_id: string;
  operation: AdminOperationItem;
  scientific: ScientificRunDetail | null;
  observed_transport?: ObservedTransport[];
}
export interface AppRuns {
  items: AppRun[];
  next_cursor: string | null;
}
export interface AppSettings {
  app_id: string;
  execution_mode: "serving" | "scientific";
  app_revision: number;
  display_name: string;
  academic_required: boolean;
  serving: ModelDeploymentRevision | null;
  scientific: ScientificModelPolicy | null;
  capabilities: AppSummary["capabilities"];
  unsupported_reason: string | null;
}
export interface AppSettingsUpdate {
  expected_app_revision: number;
  display_name?: string;
  academic_required?: boolean;
  serving_spec?: ModelDeploymentSpec;
  serving_base_etag?: string;
  scientific_policy?: ScientificModelPolicyUpdate;
}
export interface AppUsage {
  app_id: string;
  logical_runs: number;
  succeeded_runs: number;
  failed_runs: number;
  active_runs: number;
  first_used_at: string | null;
  last_used_at: string | null;
  estimated_gpu_seconds: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  scientific_gpu: {
    occupied_seconds: number | null;
    active_compute_seconds: number | null;
    occupied_idle_seconds: number | null;
  } | null;
  notes: string[];
  unique_users: number;
  users: Array<{
    user_id?: string;
    tenant_id: string;
    principal_id: string;
    logical_runs: number;
  }>;
  status_classes: Record<string, number>;
  requests_over_time: Array<{ timestamp: string; logical_runs: number }>;
  time_bucket_seconds: number;
  request_bytes: number | null;
  response_bytes: number | null;
  observed_transport?: ObservedTransportUsage | null;
}
export interface AppMetrics {
  app_id: string;
  from_at: string;
  to_at: string;
  step_seconds: number;
  charts: Array<TimeSeriesChartProps & { id: string }>;
}
export interface AppLogLine {
  at: string;
  message: string;
  namespace: string;
  pod: string;
  container: string;
  level: string | null;
  run_id: string | null;
}
export interface AppLogs {
  app_id: string;
  items: AppLogLine[];
  next_cursor: string | null;
  state: string;
  source: string;
  reason: string | null;
  truncated: boolean;
}
export interface AppContainer {
  id: string;
  pod_uid: string;
  pod_name: string;
  namespace: string;
  container_name: string;
  state: string;
  reason: string | null;
  ready: boolean | null;
  node_name: string | null;
  image: string;
  started_at: string | null;
  finished_at: string | null;
  restarts: number | null;
  gpu_resources: Record<string, number>;
  run_id: string | null;
}
export interface AppContainers {
  app_id: string;
  items: AppContainer[];
  total: number;
  truncated: boolean;
  source: string;
  state: string;
  reason: string | null;
}
