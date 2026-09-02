export type ModelDeploymentDesiredState = "Enabled" | "Draining" | "Disabled";
export type ModelDeploymentTopologyPolicy = "Any" | "SingleNode" | "HighBandwidthDomain";
export type ModelDeploymentCacheTier = "Disabled" | "ObjectStore" | "SharedFilesystem" | "NodeLocal";
export type ModelDeploymentSnapshotPreference = "Never" | "Prefer" | "Require";
export type ModelDeploymentSnapshotStrategy = "Weights" | "RuntimeNative" | "CudaCheckpoint";
export type ModelDeploymentRolloutStrategy = "Rolling" | "Recreate";
export type ModelDeploymentVisibility = "Private" | "Tenant";
export type ModelDeploymentAdoptionMode = "None" | "Observe" | "Claim";

export interface ModelDeploymentNamedDigest {
  name: string;
  digest: string;
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
  | "Progressing";

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
