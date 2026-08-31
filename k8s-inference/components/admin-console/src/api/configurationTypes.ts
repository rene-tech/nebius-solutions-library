export type ConfigurationOwner = "runtime-reconciler" | "terraform";
export type ConfigurationPlanState = "valid" | "rejected" | "superseded";
export type ReconciliationPhase =
  | "pending"
  | "awaiting-terraform-plan-apply"
  | "rendering"
  | "applying"
  | "verifying"
  | "succeeded"
  | "failed"
  | "rolled-back";
export type ValidationSeverity = "error" | "warning";
export type SnapshotStrategy = "disabled" | "cuda-checkpoint" | "runtime-native" | "weights";
export type CacheTier = "object-store" | "shared-filesystem" | "node-local";

export interface TolerationConfiguration {
  key: string;
  operator: "Equal" | "Exists";
  value: string | null;
  effect: "NoSchedule" | "PreferNoSchedule" | "NoExecute" | null;
  toleration_seconds: number | null;
}

export interface AcceleratorPoolConfiguration {
  resource_name: string;
  accelerator_class: string;
  capacity_type: string;
  accelerators_per_node: number;
  min_nodes: number;
  max_nodes: number;
  node_selector: Record<string, string>;
  tolerations: TolerationConfiguration[];
}

export interface PlacementConfiguration {
  pool_ids: string[];
  accelerators: number;
  topology_policy: "any" | "single-node" | "nvlink-domain";
}

export interface AutoscalingConfiguration {
  min_replicas: number;
  max_replicas: number;
  target_queue_depth: number;
  polling_interval_seconds: number;
  cooldown_seconds: number;
}

export interface QueueConfiguration {
  local_queue: string;
  priority_class: string;
  max_queue_seconds: number;
}

export interface SnapshotConfiguration {
  strategy: SnapshotStrategy;
  cache_tier: CacheTier;
  restore_timeout_seconds: number;
  parallelism: number;
  require_semantic_check: boolean;
}

export interface McpConfiguration {
  exposed: boolean;
  tool_name: string | null;
}

export interface RateConfiguration {
  requests_per_minute: number | null;
  concurrent_requests: number;
  accelerator_seconds_per_day: number | null;
}

export interface ArtifactIdentity {
  image_repository: string;
  image_digest: string;
  model_revision: string;
  artifact_manifest_sha256: string | null;
  acquisition_contract_sha256: string;
  provenance_sha256: string;
  semantic_health_contract_sha256: string;
}

export interface ModelConfiguration {
  model_id: string;
  enabled: boolean;
  placement: PlacementConfiguration;
  autoscaling: AutoscalingConfiguration;
  queue: QueueConfiguration;
  snapshot: SnapshotConfiguration;
  mcp: McpConfiguration;
  rate: RateConfiguration;
  artifact: ArtifactIdentity;
}

export interface PlatformConfiguration {
  schema_version: "fs2.admin-configuration/v1";
  pools: Record<string, AcceleratorPoolConfiguration>;
  models: Record<string, ModelConfiguration>;
}

export interface ConfigurationRevision {
  revision: number;
  etag: string;
  desired: PlatformConfiguration;
  effective: PlatformConfiguration;
  created_at: string;
  created_by: string;
  previous_revision: number | null;
  reconciliation_id: string | null;
}

export interface ConfigurationProposal {
  base_etag: string;
  desired: PlatformConfiguration;
}

export interface ConfigurationChange {
  path: string;
  owner: ConfigurationOwner;
  before: unknown;
  after: unknown;
}

export interface ConfigurationDiff {
  base_revision: number;
  base_etag: string;
  proposed_etag: string;
  changes: ConfigurationChange[];
  runtime_change_count: number;
  terraform_change_count: number;
}

export interface ConfigurationValidationIssue {
  severity: ValidationSeverity;
  code: string;
  path: string;
  message: string;
}

export interface ConfigurationValidation {
  valid: boolean;
  proposed_etag: string;
  issues: ConfigurationValidationIssue[];
}

export interface RenderedConfigurationArtifact {
  kind: string;
  name: string;
  sha256: string;
  source: string;
}

export interface TerraformHandoff {
  required: boolean;
  state: "not-required" | "review-required";
  variables: Record<string, unknown>;
  variables_sha256: string;
  expected_source_etag: string;
  tfvars_filename: string;
  tfvars_json: string;
  tfvars_sha256: string;
  forbidden_browser_actions: string[];
}

export interface ConfigurationPlan {
  plan_id: string;
  state: ConfigurationPlanState;
  base_revision: number;
  base_etag: string;
  proposed: PlatformConfiguration;
  proposed_etag: string;
  validation: ConfigurationValidation;
  diff: ConfigurationDiff;
  artifacts: RenderedConfigurationArtifact[];
  terraform: TerraformHandoff;
  created_at: string;
  expires_at: string;
  created_by: string;
}

export interface ReconcileRequest {
  plan_id: string;
  base_etag: string;
}

export interface ReconciliationStatus {
  reconciliation_id: string;
  plan_id: string;
  phase: ReconciliationPhase;
  base_revision: number;
  target_etag: string;
  applied_revision: number | null;
  previous_revision: number | null;
  artifact_sha256: string[];
  terraform_variables_sha256: string;
  started_at: string;
  completed_at: string | null;
  error_code: string | null;
}

export interface RollbackRequest {
  target_revision: number;
  base_etag: string;
}

export interface RollbackPlan {
  target_revision: number;
  plan: ConfigurationPlan;
}
