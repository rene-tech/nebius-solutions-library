import type {
  ModelDeploymentMutationCapabilities,
  ModelDeploymentMutationResult,
  ModelDeploymentRenderPreview,
  ModelDeploymentRevision,
  ModelDeploymentSpec,
  ModelDeploymentStatusView,
  ModelDeploymentValidationPreview,
} from "../api/modelDeploymentTypes.ts";

const digest = (value: string) => `sha256:${value.repeat(64)}`;

export const modelDeploymentSpecFixture: ModelDeploymentSpec = {
  modelRef: "qwen3-8b",
  tenantId: "tenant-fixture",
  lifecycle: { desiredState: "Enabled" },
  artifact: {
    revision: "85c49b60d3e0b0182a59ee43a34a6d7036981284",
    manifestDigest: digest("a"),
    storageRef: { kind: "LocalModelCache", name: "qwen3-8b-cache" },
  },
  runtime: {
    profile: "vllm-openai",
    image: `registry.example.invalid/fs2/qwen@${digest("b")}`,
    templateRef: { name: "vllm-openai-v1", digest: digest("c") },
  },
  placement: {
    poolRefs: ["preemptible-h100", "reserved-h100"],
    acceleratorsPerReplica: 1,
    topologyPolicy: "SingleNode",
  },
  availability: {
    minReplicas: 0,
    maxReplicas: 4,
    idleSeconds: 300,
    targetQueueDepth: 2,
    pollingIntervalSeconds: 5,
    cooldownSeconds: 300,
    warmWindows: [{
      name: "business-hours",
      schedule: "0 8 * * 1-5",
      timeZone: "UTC",
      durationSeconds: 36000,
      minReplicas: 1,
    }],
  },
  cache: {
    tier: "NodeLocal",
    snapshotPreference: "Prefer",
    snapshotRef: { name: "qwen3-8b-weights", digest: digest("d"), strategy: "Weights" },
  },
  queue: { localQueue: "inference", priorityClass: "interactive", maxQueueSeconds: 900 },
  rollout: { strategy: "Rolling", maxUnavailable: 0, maxSurge: 1, progressDeadlineSeconds: 1800 },
  exposure: { openAI: true, openAIAliases: ["qwen3-8b"], mcp: true, mcpToolName: "qwen3_8b_chat" },
  policy: {
    visibility: "Tenant",
    policyRef: "tenant-default.v1",
    allowedPrincipalIds: ["research-agent"],
    ratePolicyRef: null,
  },
  adoption: { mode: "None", receiptRef: null },
};

export const modelDeploymentRevisionFixture: ModelDeploymentRevision = {
  namespace: "fs2-models",
  name: "qwen-live",
  tenant_id: "tenant-fixture",
  revision: 2,
  etag: digest("e"),
  spec: modelDeploymentSpecFixture,
  action: "update",
  created_at: "2026-09-02T07:30:00Z",
  created_by: "operator@example.test",
  previous_revision: 1,
};

export const modelDeploymentStatusFixture: ModelDeploymentStatusView = {
  namespace: "fs2-models",
  name: "qwen-live",
  revision: 2,
  etag: digest("e"),
  state: "observed",
  reason: null,
  observation: {
    observation_id: "11111111-1111-4111-8111-111111111111",
    namespace: "fs2-models",
    name: "qwen-live",
    tenant_id: "tenant-fixture",
    revision: 2,
    observed_at: "2026-09-02T07:31:00Z",
    status: {
      observed_generation: 2,
      phase: "Ready",
      spec_digest: digest("e"),
      render_digest: digest("f"),
      active_revision: modelDeploymentSpecFixture.artifact.revision,
      admitted_pool_ref: "reserved-h100",
      eligible_pool_refs: ["preemptible-h100", "reserved-h100"],
      placements: [{ deployment_name: "qwen3-8b-hot-reserved-h100", pool_ref: "reserved-h100", role: "hot", desired: 1, ready: 1, available: 1 }],
      replicas: { desired: 1, admitted: 1, node_pending: 0, localizing: 0, runtime_starting: 0, warming: 0, ready: 1, available: 1 },
      cache: { state: "Cached", tier: "NodeLocal", digest: digest("d"), observed_at: "2026-09-02T07:30:30Z" },
      publication: { open_ai: true, mcp: true, observed_at: "2026-09-02T07:31:00Z" },
      endpoint: { namespace: "fs2-models", service_name: "qwen-live", service_port: 8000, uid: "service-uid-fixture", digest: digest("8") },
      adoption: { state: "None", receipt_digest: null },
      resources: [],
      infrastructure_handoff: null,
      retry_count: 0,
      last_reconcile_time: "2026-09-02T07:31:00Z",
      conditions: [{ type: "Ready", status: "True", observed_generation: 2, reason: "RuntimeReady", message: "The qualified runtime is ready and published.", last_transition_time: "2026-09-02T07:31:00Z" }],
    },
  },
};

const decision = {
  disposition: "accepted" as const,
  specDigest: digest("e"),
  issues: [],
  terraformInputs: [],
  admittedPoolRef: "reserved-h100",
};

export const modelDeploymentValidationFixture: ModelDeploymentValidationPreview = {
  schema_version: "fs2-serve.nebius.ai/model-deployment-validation-preview/v1",
  name: "qwen-live",
  namespace: "fs2-models",
  current_revision: 2,
  current_etag: digest("e"),
  decision,
  mutation_supported: false,
};

export const modelDeploymentPlanFixture: ModelDeploymentRenderPreview = {
  schema_version: "fs2-serve.nebius.ai/model-deployment-render-preview/v1",
  preview_id: "22222222-2222-4222-8222-222222222222",
  name: "qwen-live",
  namespace: "fs2-models",
  base_etag: digest("e"),
  proposed_etag: digest("e"),
  decision,
  render: {
    renderer: "legacy-manifest-v1",
    specDigest: digest("e"),
    renderDigest: digest("f"),
    resources: [{
      apiVersion: "apps/v1",
      kind: "Deployment",
      namespace: "fs2-models",
      name: "qwen-live",
      manifest: {},
      digest: digest("9"),
      fieldManager: "fs2-model-controller",
      forceConflicts: false,
    }],
    endpoint: { namespace: "fs2-models", serviceName: "qwen-live", servicePort: 8000 },
  },
  created_at: "2026-09-02T07:32:00Z",
  expires_at: "2099-09-02T07:47:00Z",
  mutation_supported: true,
  blocked_actions: [],
};

export const modelDeploymentMutationCapabilitiesFixture: ModelDeploymentMutationCapabilities = {
  schema_version: "fs2-serve.nebius.ai/model-deployment-mutations/v1",
  declarative_apply: { enabled: true, reason: null },
  drain: { enabled: true, reason: null },
  rollback: { enabled: true, reason: null },
  reconcile: { enabled: true, reason: null },
  hard_delete: {
    enabled: false,
    reason: "drain and retain revision history; hard deletion is not enabled",
  },
  configuration_revision: digest("6"),
  configuration_options: [{
    model_ref: "qwen3-8b",
    suggested_name: "qwen3-8b-live",
    namespace: "fs2-models",
    default_spec: {
      ...modelDeploymentSpecFixture,
      placement: {
        ...modelDeploymentSpecFixture.placement,
        poolRefs: ["reserved-h100", "preemptible-h100"],
      },
    },
    pool_choices: [
      {
        pool_ref: "reserved-h100",
        accelerator_class: "nvidia-h100-sxm5-80gb",
        capacity_type: "regular",
        accelerators_per_node: 8,
        maximum_replicas: 16,
      },
      {
        pool_ref: "preemptible-h100",
        accelerator_class: "nvidia-h100-sxm5-80gb",
        capacity_type: "preemptible",
        accelerators_per_node: 8,
        maximum_replicas: 8,
      },
    ],
    local_queue_choices: ["inference"],
    priority_class_choices: ["interactive", "standard"],
    tenant_choices: ["tenant-fixture"],
    scale_to_zero_qualified: true,
  }],
};

export const modelDeploymentAppliedFixture: ModelDeploymentMutationResult = {
  revision: {
    ...modelDeploymentRevisionFixture,
    revision: 3,
    etag: digest("7"),
    previous_revision: 2,
    created_at: "2026-09-02T07:33:00Z",
  },
  idempotent_replay: false,
  projection: "applied",
  receipt: {
    namespace: "fs2-models",
    name: "qwen-live",
    uid: "model-deployment-uid-fixture",
    resource_version: "314",
    generation: 3,
    spec_digest: digest("7"),
  },
  reason: null,
};

export const modelDeploymentPendingFixture: ModelDeploymentMutationResult = {
  ...modelDeploymentAppliedFixture,
  projection: "pending",
  receipt: null,
  reason: "desired revision is durable; Kubernetes projection is pending retry",
};
