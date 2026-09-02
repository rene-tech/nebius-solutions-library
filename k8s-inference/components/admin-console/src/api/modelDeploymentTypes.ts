export type ModelDeploymentDesiredState = "Enabled" | "Draining" | "Disabled";
export type ModelDeploymentTopologyPolicy = "Any" | "SingleNode" | "HighBandwidthDomain";
export type ModelDeploymentCacheTier = "Disabled" | "ObjectStore" | "SharedFilesystem" | "NodeLocal";
export type ModelDeploymentSnapshotPreference = "Never" | "Prefer" | "Require";
export type ModelDeploymentSnapshotStrategy = "Weights" | "RuntimeNative" | "CudaCheckpoint";
export type ModelDeploymentFastStartLevel = "Off" | "L1" | "L2" | "L3" | "L4";
export type ModelDeploymentEffectiveFastStartLevel = ModelDeploymentFastStartLevel | "Hot";
export type ModelDeploymentFastStartMode = "Fixed" | "Automatic";
export type ModelDeploymentFastStartFallbackPolicy = "AllowLowerLevel" | "RequireTarget";
export type ModelDeploymentRolloutStrategy = "Rolling" | "Recreate";
export type ModelDeploymentVisibility = "Private" | "Tenant";
export type ModelDeploymentAdoptionMode = "None" | "Observe" | "Claim";

export interface ModelDeploymentNamedDigest {
  name: string;
  digest: string;
}

export interface ModelDeploymentFastStartPolicy {
  mode: ModelDeploymentFastStartMode;
  level?: ModelDeploymentFastStartLevel;
  minimumLevel?: ModelDeploymentFastStartLevel;
  maximumLevel?: ModelDeploymentFastStartLevel;
  fallbackPolicy?: ModelDeploymentFastStartFallbackPolicy;
}

export interface ModelDeploymentSpec {
  modelRef: string;
  tenantId: string;
  lifecycle: {
    desiredState: ModelDeploymentDesiredState;
  };
  artifact: {
    revision: string;
    manifestDigest: string;
    storageRef: {
      kind: "ObjectStore" | "PersistentVolumeClaim" | "LocalModelCache";
      name: string;
    } | null;
  };
  runtime: {
    profile: string;
    image: string;
    templateRef: ModelDeploymentNamedDigest;
  };
  placement: {
    poolRefs: string[];
    acceleratorsPerReplica: number;
    topologyPolicy: ModelDeploymentTopologyPolicy;
  };
  availability: {
    minReplicas: number;
    maxReplicas: number;
    idleSeconds: number;
    targetQueueDepth: number;
    pollingIntervalSeconds: number;
    cooldownSeconds: number;
    warmWindows: Array<{
      name: string;
      schedule: string;
      timeZone: string;
      durationSeconds: number;
      minReplicas: number;
    }>;
  };
  cache: {
    tier: ModelDeploymentCacheTier;
    snapshotPreference: ModelDeploymentSnapshotPreference;
    snapshotRef: (ModelDeploymentNamedDigest & { strategy: ModelDeploymentSnapshotStrategy }) | null;
  };
  /** Optional while older ModelDeployment revisions are still being migrated. */
  fastStart?: ModelDeploymentFastStartPolicy | null;
  queue: {
    localQueue: string;
    priorityClass: string;
    maxQueueSeconds: number;
  };
  rollout: {
    strategy: ModelDeploymentRolloutStrategy;
    maxUnavailable: number;
    maxSurge: number;
    progressDeadlineSeconds: number;
  };
  exposure: {
    openAI: boolean;
    openAIAliases: string[];
    mcp: boolean;
    mcpToolName: string | null;
  };
  policy: {
    visibility: ModelDeploymentVisibility;
    policyRef: string;
    allowedPrincipalIds: string[];
    ratePolicyRef: string | null;
  };
  adoption: {
    mode: ModelDeploymentAdoptionMode;
    receiptRef: ModelDeploymentNamedDigest | null;
  };
}

export type ModelDeploymentRevisionAction = "create" | "update" | "rollback";

export interface ModelDeploymentRevision {
  namespace: string;
  name: string;
  tenant_id: string;
  revision: number;
  etag: string;
  spec: ModelDeploymentSpec;
  action: ModelDeploymentRevisionAction;
  created_at: string;
  created_by: string;
  previous_revision: number | null;
}

export interface ModelDeploymentList {
  items: ModelDeploymentRevision[];
  next_after: string | null;
}

export interface ModelDeploymentHistory {
  items: ModelDeploymentRevision[];
  next_before_revision: number | null;
}

export type ModelDeploymentRuntimePhase =
  | "Desired"
  | "Admitted"
  | "NodePending"
  | "Localizing"
  | "RuntimeStarting"
  | "Warming"
  | "Ready"
  | "Cold"
  | "Draining"
  | "Failed"
  | "InfrastructureRequired";

export type ModelDeploymentCacheState = "Unknown" | "Missing" | "Localizing" | "Cached" | "Failed";
export type ModelDeploymentCacheStatusTier = "ObjectStore" | "SharedFilesystem" | "NodeLocal";
export type ModelDeploymentAdoptionState = "None" | "ObserveOnly" | "Claiming" | "Owned" | "RollingBack" | "Failed";
export type ModelDeploymentConditionType =
  | "Ready"
  | "Cached"
  | "Cold"
  | "Loading"
  | "Draining"
  | "InfrastructureRequired"
  | "Failed"
  | "Progressing"
  | "FastStartQualified";

export interface ModelDeploymentFastStartStatistics {
  sampleCount?: number | null;
  sample_count?: number | null;
  failedCount?: number | null;
  failed_count?: number | null;
  latestSeconds?: number | null;
  latest_seconds?: number | null;
  latestObservedAt?: string | null;
  latest_observed_at?: string | null;
  p50Seconds?: number | null;
  p50_seconds?: number | null;
  p95Seconds?: number | null;
  p95_seconds?: number | null;
}

export interface ModelDeploymentFastStartPathStatus {
  mechanism?: string | null;
  compatibilityTupleDigest?: string | null;
  compatibility_tuple_digest?: string | null;
  qualifiedLevel?: ModelDeploymentFastStartLevel | null;
  qualified_level?: ModelDeploymentFastStartLevel | null;
  reason?: string | null;
  receiptDigests?: string[] | null;
  receipt_digests?: string[] | null;
  modelStart?: ModelDeploymentFastStartStatistics | null;
  model_start?: ModelDeploymentFastStartStatistics | null;
  capacityWait?: ModelDeploymentFastStartStatistics | null;
  capacity_wait?: ModelDeploymentFastStartStatistics | null;
  endToEnd?: ModelDeploymentFastStartStatistics | null;
  end_to_end?: ModelDeploymentFastStartStatistics | null;
}

export interface ModelDeploymentFastStartPoolStatus {
  poolRef?: string | null;
  pool_ref?: string | null;
  acceleratorClass?: string | null;
  accelerator_class?: string | null;
  qualifiedLevel?: ModelDeploymentFastStartLevel | null;
  qualified_level?: ModelDeploymentFastStartLevel | null;
  reason?: string | null;
  mechanisms?: string[] | null;
  selectedMechanism?: string | null;
  selected_mechanism?: string | null;
  selectedCompatibilityTupleDigest?: string | null;
  selected_compatibility_tuple_digest?: string | null;
  receiptDigests?: string[] | null;
  receipt_digests?: string[] | null;
  modelStart?: ModelDeploymentFastStartStatistics | null;
  model_start?: ModelDeploymentFastStartStatistics | null;
  capacityWait?: ModelDeploymentFastStartStatistics | null;
  capacity_wait?: ModelDeploymentFastStartStatistics | null;
  endToEnd?: ModelDeploymentFastStartStatistics | null;
  end_to_end?: ModelDeploymentFastStartStatistics | null;
  paths?: ModelDeploymentFastStartPathStatus[] | null;
}

export interface ModelExpressPoolTransportStatus {
  mode?: "fallback" | "nixl-rdma" | null;
  rdmaResourceName?: string | null;
  rdma_resource_name?: string | null;
  rdmaResourceQuantity?: number | null;
  rdma_resource_quantity?: number | null;
  nixlBackend?: "UCX" | "LIBFABRIC" | null;
  nixl_backend?: "UCX" | "LIBFABRIC" | null;
  rdmaNicPin?: string | null;
  rdma_nic_pin?: string | null;
}

export interface ModelExpressMechanismStatus {
  state?: "Pending" | "Configured" | null;
  configDigest?: string | null;
  config_digest?: string | null;
  deploymentMode?: "managed" | "external" | null;
  deployment_mode?: "managed" | "external" | null;
  endpoint?: string | null;
  metadataBackend?: "kubernetes" | "redis" | null;
  metadata_backend?: "kubernetes" | "redis" | null;
  runtimeAdapter?: "vllm" | null;
  runtime_adapter?: "vllm" | null;
  clientPackageVersion?: "0.5.1" | null;
  client_package_version?: "0.5.1" | null;
  coordinatorNetworkType?: "pod-selector" | "ip-blocks" | null;
  coordinator_network_type?: "pod-selector" | "ip-blocks" | null;
  coordinatorNamespace?: string | null;
  coordinator_namespace?: string | null;
  coordinatorPodLabels?: Record<string, string> | null;
  coordinator_pod_labels?: Record<string, string> | null;
  coordinatorCidrs?: string[] | null;
  coordinator_cidrs?: string[] | null;
  poolRefs?: string[] | null;
  pool_refs?: string[] | null;
  poolTransports?: Record<string, ModelExpressPoolTransportStatus> | null;
  pool_transports?: Record<string, ModelExpressPoolTransportStatus> | null;
  configurationObserved?: boolean | null;
  configuration_observed?: boolean | null;
  telemetryState?: "Unavailable" | null;
  telemetry_state?: "Unavailable" | null;
  selectedPath?: string | null;
  selected_path?: string | null;
  transferredBytes?: number | null;
  transferred_bytes?: number | null;
  transferSeconds?: number | null;
  transfer_seconds?: number | null;
  fallbackReason?: string | null;
  fallback_reason?: string | null;
}

export interface ModelDeploymentFastStartStatus {
  mode?: ModelDeploymentFastStartMode | null;
  fallbackPolicy?: ModelDeploymentFastStartFallbackPolicy | null;
  fallback_policy?: ModelDeploymentFastStartFallbackPolicy | null;
  requestedLevel?: ModelDeploymentFastStartLevel | null;
  requested_level?: ModelDeploymentFastStartLevel | null;
  minimumLevel?: ModelDeploymentFastStartLevel | null;
  minimum_level?: ModelDeploymentFastStartLevel | null;
  maximumLevel?: ModelDeploymentFastStartLevel | null;
  maximum_level?: ModelDeploymentFastStartLevel | null;
  assignedLevel?: ModelDeploymentFastStartLevel | null;
  assigned_level?: ModelDeploymentFastStartLevel | null;
  effectiveLevel?: ModelDeploymentEffectiveFastStartLevel | null;
  effective_level?: ModelDeploymentEffectiveFastStartLevel | null;
  qualifiedLevel?: ModelDeploymentFastStartLevel | null;
  qualified_level?: ModelDeploymentFastStartLevel | null;
  state?: string | null;
  reason?: string | null;
  qualification?: {
    state?: string | null;
    reason?: string | null;
    message?: string | null;
  } | null;
  targetSeconds?: number | null;
  target_seconds?: number | null;
  requestedTargetSeconds?: number | null;
  requested_target_seconds?: number | null;
  lastObservedSeconds?: number | null;
  last_observed_seconds?: number | null;
  qualifiedP50Seconds?: number | null;
  qualified_p50_seconds?: number | null;
  qualifiedP95Seconds?: number | null;
  qualified_p95_seconds?: number | null;
  capacityWaitSeconds?: number | null;
  capacity_wait_seconds?: number | null;
  endToEndSeconds?: number | null;
  end_to_end_seconds?: number | null;
  observedAt?: string | null;
  observed_at?: string | null;
  mechanisms?: ({ modelexpress?: ModelExpressMechanismStatus | null } & Record<string, unknown>) | null;
  hot?: boolean | null;
  modelStart?: ModelDeploymentFastStartStatistics | null;
  model_start?: ModelDeploymentFastStartStatistics | null;
  capacityWait?: ModelDeploymentFastStartStatistics | null;
  capacity_wait?: ModelDeploymentFastStartStatistics | null;
  endToEnd?: ModelDeploymentFastStartStatistics | null;
  end_to_end?: ModelDeploymentFastStartStatistics | null;
  pools?: ModelDeploymentFastStartPoolStatus[] | null;
  automatic?: {
    reason?: string | null;
    evaluatedAt?: string | null;
    evaluated_at?: string | null;
    historyComplete?: boolean | null;
    history_complete?: boolean | null;
    mechanismId?: string | null;
    mechanism_id?: string | null;
    score?: number | null;
    pendingLevel?: ModelDeploymentFastStartLevel | null;
    pending_level?: ModelDeploymentFastStartLevel | null;
    pendingSince?: string | null;
    pending_since?: string | null;
    consecutiveWins?: number | null;
    consecutive_wins?: number | null;
    lastTransitionAt?: string | null;
    last_transition_at?: string | null;
    shortWindowRequests?: number | null;
    short_window_requests?: number | null;
    shortWindowColdActivations?: number | null;
    short_window_cold_activations?: number | null;
    shortWindowIdleGapEpisodes?: number | null;
    short_window_idle_gap_episodes?: number | null;
    longWindowRequests?: number | null;
    long_window_requests?: number | null;
    longWindowColdActivations?: number | null;
    long_window_cold_activations?: number | null;
    longWindowIdleGapEpisodes?: number | null;
    long_window_idle_gap_episodes?: number | null;
  } | null;
}

export interface ModelDeploymentObservedStatus {
  observed_generation: number;
  phase: ModelDeploymentRuntimePhase;
  spec_digest: string;
  render_digest: string | null;
  active_revision: string | null;
  admitted_pool_ref: string | null;
  eligible_pool_refs: string[];
  placements: Array<{
    deployment_name: string;
    pool_ref: string;
    role: "hot" | "burst";
    desired: number | null;
    ready: number | null;
    available: number | null;
  }>;
  replicas: {
    desired: number | null;
    admitted: number | null;
    node_pending: number | null;
    localizing: number | null;
    runtime_starting: number | null;
    warming: number | null;
    ready: number | null;
    available: number | null;
  } | null;
  cache: {
    state: ModelDeploymentCacheState;
    tier: ModelDeploymentCacheStatusTier | null;
    digest: string | null;
    observed_at: string | null;
  } | null;
  /** `fast_start` is accepted during the API's snake_case migration window. */
  fastStart?: ModelDeploymentFastStartStatus | null;
  fast_start?: ModelDeploymentFastStartStatus | null;
  publication: {
    open_ai: boolean;
    mcp: boolean;
    observed_at: string | null;
  } | null;
  endpoint: {
    namespace: string;
    service_name: string;
    service_port: number;
    uid: string;
    digest: string;
  } | null;
  adoption: {
    state: ModelDeploymentAdoptionState;
    receipt_digest: string | null;
  } | null;
  resources: Array<{
    identity: string;
    api_version: string;
    kind: string;
    namespace: string;
    name: string;
    uid: string;
    generation: number;
    digest: string | null;
  }>;
  infrastructure_handoff: {
    reason: string;
    owner: "Terraform";
    required_inputs: string[];
  } | null;
  retry_count: number;
  last_reconcile_time: string;
  conditions: Array<{
    type: ModelDeploymentConditionType;
    status: "True" | "False" | "Unknown";
    observed_generation: number;
    reason: string;
    message: string;
    last_transition_time: string;
  }>;
}

export interface ModelDeploymentStatusObservation {
  observation_id: string;
  namespace: string;
  name: string;
  tenant_id: string;
  revision: number;
  status: ModelDeploymentObservedStatus;
  observed_at: string;
}

export interface ModelDeploymentStatusView {
  namespace: string;
  name: string;
  revision: number;
  etag: string;
  state: "observed" | "stale" | "unavailable";
  observation: ModelDeploymentStatusObservation | null;
  reason: string | null;
}

export type ModelDeploymentValidationDisposition = "accepted" | "rejected" | "infrastructure-required";

export interface ModelDeploymentValidationIssue {
  severity: "error" | "warning";
  code: string;
  path: string;
  message: string;
  owner: "live-control-plane" | "terraform";
}

export interface ModelDeploymentValidationDecision {
  disposition: ModelDeploymentValidationDisposition;
  specDigest: string;
  issues: ModelDeploymentValidationIssue[];
  terraformInputs: string[];
  admittedPoolRef: string | null;
}

export interface ModelDeploymentPreviewProposal {
  name: string;
  namespace: string;
  base_etag: string | null;
  spec: ModelDeploymentSpec;
}

export interface ModelDeploymentValidationPreview {
  schema_version: "fs2-serve.nebius.ai/model-deployment-validation-preview/v1";
  name: string;
  namespace: string;
  current_revision: number | null;
  current_etag: string | null;
  decision: ModelDeploymentValidationDecision;
  mutation_supported: boolean;
}

export interface ModelDeploymentRenderedResource {
  apiVersion: string;
  kind: string;
  namespace: string;
  name: string;
  manifest: Record<string, unknown>;
  digest: string;
  fieldManager: "fs2-model-controller";
  forceConflicts: false;
}

export interface ModelDeploymentRenderPreview {
  schema_version: "fs2-serve.nebius.ai/model-deployment-render-preview/v1";
  preview_id: string;
  name: string;
  namespace: string;
  base_etag: string | null;
  proposed_etag: string;
  decision: ModelDeploymentValidationDecision;
  render: {
    renderer: string;
    specDigest: string;
    renderDigest: string;
    resources: ModelDeploymentRenderedResource[];
    endpoint: {
      namespace: string;
      serviceName: string;
      servicePort: number;
    };
  } | null;
  created_at: string;
  expires_at: string;
  mutation_supported: boolean;
  blocked_actions: Array<"apply" | "adopt" | "delete">;
}

export interface ModelDeploymentActionCapability {
  enabled: boolean;
  reason: string | null;
}

export interface ModelDeploymentPoolChoice {
  pool_ref: string;
  accelerator_class: string;
  capacity_type: string;
  accelerators_per_node: number;
  maximum_replicas: number;
}

export interface ModelDeploymentConfigurationOption {
  model_ref: string;
  suggested_name: string;
  namespace: string;
  default_spec: ModelDeploymentSpec;
  pool_choices: ModelDeploymentPoolChoice[];
  local_queue_choices: string[];
  priority_class_choices: string[];
  tenant_choices: string[];
  scale_to_zero_qualified: boolean;
}

export interface ModelDeploymentMutationCapabilities {
  schema_version: "fs2-serve.nebius.ai/model-deployment-mutations/v1";
  declarative_apply: ModelDeploymentActionCapability;
  drain: ModelDeploymentActionCapability;
  rollback: ModelDeploymentActionCapability;
  reconcile: ModelDeploymentActionCapability;
  hard_delete: ModelDeploymentActionCapability;
  configuration_revision: string;
  configuration_options: ModelDeploymentConfigurationOption[];
}

export interface DesiredWriteReceipt {
  namespace: string;
  name: string;
  uid: string;
  resource_version: string;
  generation: number;
  spec_digest: string;
}

interface ModelDeploymentMutationResultBase {
  revision: ModelDeploymentRevision;
  idempotent_replay: boolean;
}

export type ModelDeploymentMutationResult = ModelDeploymentMutationResultBase & (
  | { projection: "applied"; receipt: DesiredWriteReceipt; reason: null }
  | { projection: "pending"; receipt: null; reason: string }
);

export interface ModelDeploymentApplyRequest {
  preview_id: string;
  proposed_etag: string;
  proposal: ModelDeploymentPreviewProposal;
  idempotency_key: string;
}

export interface ModelDeploymentActionRequest {
  base_etag: string;
  idempotency_key: string;
}

export interface ModelDeploymentRollbackRequest extends ModelDeploymentActionRequest {
  target_revision: number;
}

export interface ModelDeploymentReconcileRequest {
  expected_etag: string;
}

export interface ModelDeploymentListQuery {
  namespace?: string;
  tenantId?: string;
  after?: string;
  limit?: number;
}

export interface ModelDeploymentIdentityQuery {
  namespace?: string;
  tenantId?: string;
}
