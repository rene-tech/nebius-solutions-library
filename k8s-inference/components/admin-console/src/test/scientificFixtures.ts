import type {
  ScientificAccessGate,
  ScientificBackendIdentity,
  ScientificCapabilities,
  ScientificEvidenceMeasurement,
  ScientificModelReadiness,
  ScientificModelReadinessList,
  ScientificRunDetail,
  ScientificRunList,
  ScientificRunSummary,
  ScientificStageState,
} from "../api/scientificTypes.ts";

const digest = (character: string) => `sha256:${character.repeat(64)}`;
const gitRevision = (character: string) => character.repeat(40);

const measured = (value: number, unit: ScientificEvidenceMeasurement["unit"], source: string): ScientificEvidenceMeasurement => ({
  value,
  unit,
  evidence: "measured",
  source,
  reason: null,
});

const estimated = (value: number, unit: ScientificEvidenceMeasurement["unit"], source: string, reason: string): ScientificEvidenceMeasurement => ({
  value,
  unit,
  evidence: "estimated",
  source,
  reason,
});

const unavailable = (unit: ScientificEvidenceMeasurement["unit"], source: string, reason: string): ScientificEvidenceMeasurement => ({
  value: null,
  unit,
  evidence: "unavailable",
  source,
  reason,
});

const standardAccess: ScientificAccessGate = {
  profile: "standard",
  state: "not-required",
  gate: "No restricted academic asset is required by this backend.",
  receipt_digest: null,
  request_time_license_receipt_required: false,
  authorization: null,
  formal_license_status: "not-applicable",
  credentials_exposed: false,
  alternative: null,
};

const alphaFoldAccess: ScientificAccessGate = {
  profile: "academic",
  state: "verified",
  gate: "Deployment-bound academic use is Granted and execution is Authorized; no request-time licence receipt is required. Formal acceptance is advisory and is not an admission gate.",
  receipt_digest: null,
  request_time_license_receipt_required: false,
  authorization: {
    asset_id: "alphafold3",
    backend_id: "alphafold3-native",
    license_id: "AlphaFold-3-Model-Parameters-Terms-of-Use-2024-11-09",
    use_authorization_status: "Granted",
    execution_authorization_status: "Authorized",
    serving_admission: "AdmittedNoPerRequestLicenseReceipt",
    asset_namespace: "fs2-academic-poc",
  },
  formal_license_status: "FormalAcceptancePending",
  credentials_exposed: false,
  alternative: {
    model_id: "openfold3",
    display_name: "OpenFold3",
    relationship: "explicit-alternative",
    reason: "Open alternative; it is not represented as native AlphaFold3.",
  },
};

const bindCraftAccess: ScientificAccessGate = {
  profile: "academic",
  state: "verified",
  gate: "Deployment-bound academic use is Granted and execution is Authorized; no request-time licence receipt is required. Formal acceptance is advisory and is not an admission gate.",
  receipt_digest: null,
  request_time_license_receipt_required: false,
  authorization: {
    asset_id: "pyrosetta-bindcraft",
    backend_id: "bindcraft-native-pyrosetta",
    license_id: "Rosetta-and-PyRosetta-Non-Commercial-License-Chain",
    use_authorization_status: "Granted",
    execution_authorization_status: "Authorized",
    serving_admission: "AdmittedNoPerRequestLicenseReceipt",
    asset_namespace: "fs2-academic-poc",
  },
  formal_license_status: "FormalAcceptancePending",
  credentials_exposed: false,
  alternative: {
    model_id: "bindcraft-open",
    display_name: "Open binder workflow",
    relationship: "explicit-alternative",
    reason: "Open alternative; it is not represented as native BindCraft/PyRosetta.",
  },
};

function backend(modelId: string, repository: string, character: string): ScientificBackendIdentity {
  return {
    backend_id: `${modelId}-native-h100`,
    kind: "containerized-scientific-runtime",
    source_repository: repository,
    source_revision: gitRevision(character),
    model_revision: digest(character),
    runtime_image_digest: digest(character),
    execution_identity_digest: digest(character),
  };
}

const stageCounts = (values: Partial<Record<ScientificStageState, number>>): Record<ScientificStageState, number> => ({
  pending: 0,
  queued: 0,
  admitted: 0,
  running: 0,
  succeeded: 0,
  failed: 0,
  cancelled: 0,
  skipped: 0,
  ...values,
});

const pendingGpuAccounting = {
  gpu_count: 0,
  capacity_type: "unknown" as const,
  allocated: unavailable("gpu-seconds", "lifecycle-ledger", "No GPU allocation boundary has been observed."),
  active: unavailable("gpu-seconds", "lifecycle-ledger", "No GPU active-compute interval has been observed."),
  idle_total: unavailable("gpu-seconds", "lifecycle-ledger", "No GPU allocation boundary has been observed."),
  idle_by_cause: [],
  grace_drain: unavailable("gpu-seconds", "lifecycle-ledger", "No GPU grace or drain interval has been observed."),
  reconciliation_delta: unavailable("gpu-seconds", "lifecycle-ledger", "There is no GPU lifecycle to reconcile."),
};

export const completedScientificRun: ScientificRunSummary = {
  id: "run-rfdiffusion-0001",
  batch_id: "batch-cd8-screen-0042",
  display_name: "CD8 binder backbone screen",
  operation: "generate-backbone",
  status: "succeeded",
  submitted_at: "2026-08-30T07:54:10Z",
  completed_at: "2026-08-30T08:19:43Z",
  attribution: {
    tenant_id: "tenant-oncology",
    user_id: "researcher-ada",
    principal_id: "svc-cd8-design",
    api_key_prefix: "fs2_pat_7c91",
  },
  model: {
    model_id: "rfdiffusion",
    display_name: "RFdiffusion",
    execution_mode: "scientific-batch",
    backend: backend("rfdiffusion", "RosettaCommons/RFdiffusion", "1"),
  },
  access: standardAccess,
  service_class: {
    requested: "customer-batch",
    effective: "customer-batch",
    reason: "Tenant policy accepted the requested service class without demotion.",
    policy_revision: digest("2"),
  },
  queue: {
    tenant_queue: "tenant-oncology",
    model_lane: "rfdiffusion",
    local_queue: "scientific-runs",
    cluster_queue: "inference-accelerators",
    workload_priority_class: "scientific-customer-batch",
    priority_value: 500,
    admission_state: "finished",
    admission_reason: "Admitted on preemptible H100 capacity; retry completed after eviction.",
    admitted_at: "2026-08-30T07:55:02Z",
    queue_position: measured(3, "count", "batch-controller-ledger"),
  },
  fast_start: {
    tier: "model-artifact-local",
    evidence: "observed",
    observed_at: "2026-08-30T07:56:08Z",
    runtime_identity_digest: digest("1"),
    reason: "The exact runtime image and model artifacts were local; no GPU-memory snapshot was used.",
  },
  stage_counts: stageCounts({ succeeded: 3 }),
  gpu_accounting: {
    gpu_count: 1,
    capacity_type: "preemptible",
    allocated: measured(1513, "gpu-seconds", "gpu-lifecycle-ledger"),
    active: measured(1337, "gpu-seconds", "gpu-lifecycle-ledger"),
    idle_total: measured(157, "gpu-seconds", "gpu-lifecycle-ledger"),
    idle_by_cause: [
      { cause: "artifact-load", duration: measured(91, "gpu-seconds", "gpu-lifecycle-ledger") },
      { cause: "warmup", duration: measured(42, "gpu-seconds", "gpu-lifecycle-ledger") },
      { cause: "between-stages", duration: measured(24, "gpu-seconds", "gpu-lifecycle-ledger") },
      { cause: "unattributed", duration: measured(0, "gpu-seconds", "gpu-lifecycle-ledger") },
    ],
    grace_drain: measured(19, "gpu-seconds", "gpu-lifecycle-ledger"),
    reconciliation_delta: measured(0, "gpu-seconds", "gpu-lifecycle-ledger"),
  },
  error: null,
  cancellation: {
    state: "not-requested",
    requested_at: null,
    requested_by: null,
    reason: "The run completed before cancellation was requested.",
    mode: "checkpoint-then-terminate",
    grace_seconds: 90,
    can_cancel: false,
  },
};

const pendingAlphaFoldRun: ScientificRunSummary = {
  id: "run-alphafold3-0007",
  batch_id: "batch-neoantigen-0014",
  display_name: "Neoantigen complex ranking",
  operation: "predict-complex",
  status: "queued",
  submitted_at: "2026-08-30T08:22:19Z",
  completed_at: null,
  attribution: {
    tenant_id: "tenant-oncology",
    user_id: "researcher-grace",
    principal_id: "svc-structure-ranking",
    api_key_prefix: "fs2_pat_a81e",
  },
  model: {
    model_id: "alphafold3",
    display_name: "AlphaFold3 (native)",
    execution_mode: "scientific-batch",
    backend: backend("alphafold3", "google-deepmind/alphafold3", "3"),
  },
  access: alphaFoldAccess,
  service_class: {
    requested: "interactive",
    effective: "interactive",
    reason: "The validated request was frozen into the scheduling snapshot.",
    policy_revision: digest("2"),
  },
  queue: {
    tenant_queue: "tenant-oncology",
    model_lane: "alphafold3",
    local_queue: "academic-scientific",
    cluster_queue: "inference-accelerators",
    workload_priority_class: "scientific-interactive",
    priority_value: 800,
    admission_state: "pending",
    admission_reason: "Deployment-bound academic access is authorized; runtime capacity admission is pending.",
    admitted_at: null,
    queue_position: unavailable("count", "batch-controller-ledger", "Queue position is not measured by the controller."),
  },
  fast_start: {
    tier: "not-observed",
    evidence: "unavailable",
    observed_at: null,
    runtime_identity_digest: null,
    reason: "No exact fast-start tier has been observed for this queued runtime identity.",
  },
  stage_counts: stageCounts({ pending: 3 }),
  gpu_accounting: pendingGpuAccounting,
  error: null,
  cancellation: {
    state: "not-requested",
    requested_at: null,
    requested_by: null,
    reason: null,
    mode: "terminate-attempt",
    grace_seconds: 30,
    can_cancel: true,
  },
};

const cancelledBindCraftRun: ScientificRunSummary = {
  id: "run-bindcraft-0011",
  batch_id: "batch-pdl1-binders-0008",
  display_name: "PD-L1 binder refinement",
  operation: "design-binder",
  status: "cancelled",
  submitted_at: "2026-08-30T07:31:02Z",
  completed_at: "2026-08-30T07:48:51Z",
  attribution: {
    tenant_id: "tenant-translational",
    user_id: "researcher-katherine",
    principal_id: "svc-binder-design",
    api_key_prefix: "fs2_pat_f414",
  },
  model: {
    model_id: "bindcraft",
    display_name: "BindCraft (native PyRosetta)",
    execution_mode: "hybrid",
    backend: backend("bindcraft", "martinpacesa/BindCraft", "4"),
  },
  access: bindCraftAccess,
  service_class: {
    requested: "bulk-backfill",
    effective: "bulk-backfill",
    reason: "Bulk backfill remained interruptible under the tenant policy.",
    policy_revision: digest("2"),
  },
  queue: {
    tenant_queue: "tenant-translational",
    model_lane: "bindcraft-native",
    local_queue: "scientific-runs",
    cluster_queue: "inference-accelerators",
    workload_priority_class: "scientific-bulk-backfill",
    priority_value: 100,
    admission_state: "finished",
    admission_reason: "Cancellation was acknowledged after the checkpoint grace period.",
    admitted_at: "2026-08-30T07:36:21Z",
    queue_position: measured(7, "count", "batch-controller-ledger"),
  },
  fast_start: {
    tier: "container-image-local",
    evidence: "observed",
    observed_at: "2026-08-30T07:37:04Z",
    runtime_identity_digest: digest("4"),
    reason: "Only the exact container image was local; artifacts were loaded conventionally.",
  },
  stage_counts: stageCounts({ succeeded: 1, cancelled: 1, skipped: 1 }),
  gpu_accounting: {
    gpu_count: 1,
    capacity_type: "capacity-block",
    allocated: estimated(721, "gpu-seconds", "operation-duration-projection", "GPU lifecycle events were incomplete for the cancelled attempt."),
    active: measured(492, "gpu-seconds", "gpu-lifecycle-ledger"),
    idle_total: unavailable("gpu-seconds", "gpu-lifecycle-ledger", "Idle causes cannot be reconciled from the incomplete allocation boundary."),
    idle_by_cause: [
      { cause: "artifact-load", duration: measured(83, "gpu-seconds", "gpu-lifecycle-ledger") },
      { cause: "unattributed", duration: unavailable("gpu-seconds", "gpu-lifecycle-ledger", "Allocation start was not observed.") },
    ],
    grace_drain: measured(31, "gpu-seconds", "gpu-lifecycle-ledger"),
    reconciliation_delta: unavailable("gpu-seconds", "gpu-lifecycle-ledger", "Allocated GPU time is estimated and cannot be reconciled."),
  },
  error: null,
  cancellation: {
    state: "acknowledged",
    requested_at: "2026-08-30T07:47:58Z",
    requested_by: "researcher-katherine",
    reason: "Input target set was superseded.",
    mode: "checkpoint-then-terminate",
    grace_seconds: 45,
    can_cancel: false,
  },
};

export const scientificRunListFixture: ScientificRunList = {
  items: [completedScientificRun, pendingAlphaFoldRun, cancelledBindCraftRun],
  next_cursor: null,
};

export const scientificRunDetailFixture: ScientificRunDetail = {
  run: completedScientificRun,
  lifecycle_phases: [
    { phase: "queue", duration: measured(52, "seconds", "batch-controller-ledger") },
    { phase: "admission", duration: measured(11, "seconds", "kueue-events") },
    { phase: "image-pull", duration: measured(0, "seconds", "kubelet-events") },
    { phase: "artifact-load", duration: measured(91, "seconds", "runtime-markers") },
    { phase: "restore", duration: measured(0, "seconds", "runtime-markers") },
    { phase: "semantic-warmup", duration: measured(42, "seconds", "runtime-markers") },
    { phase: "active-compute", duration: measured(1337, "seconds", "gpu-lifecycle-ledger") },
    { phase: "allocated-idle", duration: measured(157, "seconds", "gpu-lifecycle-ledger") },
    { phase: "grace-drain", duration: measured(19, "seconds", "gpu-lifecycle-ledger") },
    { phase: "teardown", duration: measured(7, "seconds", "kubernetes-events") },
  ],
  stages: [
    {
      id: "validate-input",
      display_name: "Validate input manifest",
      ordinal: 1,
      needs: [],
      resource_class: "cpu",
      admission_mode: "independent-jobs",
      checkpoint_mode: "none",
      status: "succeeded",
      attempts: [{
        id: "attempt-validate-1",
        number: 1,
        status: "succeeded",
        started_at: "2026-08-30T07:55:13Z",
        completed_at: "2026-08-30T07:55:19Z",
        workload_uid: "fixture-workload-validate",
        job_uid: "fixture-job-validate",
        pod_count: 1,
        node_count: 1,
        gpu_count: 0,
        admitted_at: "2026-08-30T07:55:13Z",
        resolved_pool_id: null,
        admitted_resource_flavor: null,
        accelerator_resource_name: null,
        checkpoint_input_artifact_id: null,
        checkpoint_output_artifact_id: null,
        error: null,
      }],
    },
    {
      id: "diffuse-backbone",
      display_name: "Diffuse candidate backbones",
      ordinal: 2,
      needs: ["validate-input"],
      resource_class: "gpu",
      admission_mode: "independent-jobs",
      checkpoint_mode: "restart",
      status: "succeeded",
      attempts: [
        {
          id: "attempt-diffuse-1",
          number: 1,
          status: "preempted",
          started_at: "2026-08-30T07:56:08Z",
          completed_at: "2026-08-30T08:01:31Z",
          workload_uid: "fixture-workload-diffuse-1",
          job_uid: "fixture-job-diffuse-1",
          pod_count: 1,
          node_count: 1,
          gpu_count: 1,
          admitted_at: "2026-08-30T07:56:08Z",
          resolved_pool_id: "h100-preemptible",
          admitted_resource_flavor: "inference-h100-1x",
          accelerator_resource_name: "nvidia.com/gpu",
          checkpoint_input_artifact_id: null,
          checkpoint_output_artifact_id: null,
          error: { code: "PREEMPTED", message: "Preemptible capacity was reclaimed.", retryable: true },
        },
        {
          id: "attempt-diffuse-2",
          number: 2,
          status: "succeeded",
          started_at: "2026-08-30T08:02:14Z",
          completed_at: "2026-08-30T08:19:20Z",
          workload_uid: "fixture-workload-diffuse-2",
          job_uid: "fixture-job-diffuse-2",
          pod_count: 1,
          node_count: 1,
          gpu_count: 1,
          admitted_at: "2026-08-30T08:02:14Z",
          resolved_pool_id: "h100-capacity-block",
          admitted_resource_flavor: "inference-h100-reserved-8x",
          accelerator_resource_name: "nvidia.com/gpu",
          checkpoint_input_artifact_id: null,
          checkpoint_output_artifact_id: "artifact-rfdiffusion-structures",
          error: null,
        },
      ],
    },
    {
      id: "semantic-validation",
      display_name: "Validate generated structures",
      ordinal: 3,
      needs: ["diffuse-backbone"],
      resource_class: "cpu",
      admission_mode: "independent-jobs",
      checkpoint_mode: "none",
      status: "succeeded",
      attempts: [{
        id: "attempt-semantic-1",
        number: 1,
        status: "succeeded",
        started_at: "2026-08-30T08:19:21Z",
        completed_at: "2026-08-30T08:19:43Z",
        workload_uid: "fixture-workload-semantic",
        job_uid: "fixture-job-semantic",
        pod_count: 1,
        node_count: 1,
        gpu_count: 0,
        admitted_at: "2026-08-30T08:19:21Z",
        resolved_pool_id: null,
        admitted_resource_flavor: null,
        accelerator_resource_name: null,
        checkpoint_input_artifact_id: "artifact-rfdiffusion-structures",
        checkpoint_output_artifact_id: "artifact-semantic-receipt",
        error: null,
      }],
    },
  ],
  artifacts: [
    {
      artifact_id: "artifact-input-target",
      name: "target-structure.pdb",
      role: "input",
      semantic_type: "protein-structure/v1",
      state: "available",
      sha256: "5".repeat(64),
      size_bytes: measured(82413, "bytes", "artifact-manifest"),
      media_type: "chemical/x-pdb",
      created_at: "2026-08-30T07:53:58Z",
      download: { available: true, href: "/admin/artifacts/artifact-input-target", reason: null },
    },
    {
      artifact_id: "artifact-rfdiffusion-structures",
      name: "candidate-backbones.tar.zst",
      role: "output",
      semantic_type: "backbone-candidates/v1",
      state: "available",
      sha256: "6".repeat(64),
      size_bytes: measured(14577932, "bytes", "artifact-manifest"),
      media_type: "application/zstd",
      created_at: "2026-08-30T08:19:20Z",
      download: { available: true, href: "/admin/artifacts/artifact-rfdiffusion-structures", reason: null },
    },
    {
      artifact_id: "artifact-semantic-receipt",
      name: "semantic-validation.json",
      role: "validation",
      semantic_type: "semantic-validation/v1",
      state: "available",
      sha256: "7".repeat(64),
      size_bytes: measured(2941, "bytes", "artifact-manifest"),
      media_type: "application/json",
      created_at: "2026-08-30T08:19:43Z",
      download: { available: false, href: null, reason: "Validation receipts are retained for audit and are not directly downloadable." },
    },
  ],
  retry: { max_attempts_per_stage: 3, retryable_exit_codes: [137, 143] },
  semantic_validation: { validator_id: "rfdiffusion-structure-v1", status: "passed", receipt_digest: digest("7") },
  observability: [
    { kind: "trace", label: "Request trace", available: true, href: "/admin/observability?operation_id=run-rfdiffusion-0001&signal=trace", reason: null },
    { kind: "logs", label: "Correlated logs", available: true, href: "/admin/observability?operation_id=run-rfdiffusion-0001&signal=logs", reason: null },
    { kind: "metrics", label: "GPU and queue metrics", available: true, href: "/admin/observability?operation_id=run-rfdiffusion-0001&signal=metrics", reason: null },
  ],
  payloads_exposed: false,
};

interface ReadinessSeed {
  modelId: string;
  displayName: string;
  repository: string;
  character: string;
  mode: ScientificModelReadiness["execution_mode"];
  readiness?: ScientificModelReadiness["readiness"];
  access?: ScientificAccessGate;
  interactive?: boolean;
  tier?: ScientificModelReadiness["caching"]["exact_tier"];
}

function readiness(seed: ReadinessSeed): ScientificModelReadiness {
  const access = seed.access ?? standardAccess;
  const state = seed.readiness ?? (access.state === "blocked" ? "blocked" : "candidate");
  return {
    model_id: seed.modelId,
    candidate_id: seed.modelId,
    display_name: seed.displayName,
    readiness: state,
    readiness_reason: state === "blocked"
      ? "Admission is blocked by the named access gate."
      : state === "qualified"
        ? "Runtime, semantic validator, and H100 execution evidence are qualified."
        : "The model contract is present; live semantic qualification is still pending.",
    workload_profile: "published",
    missing_evidence: [],
    qualification: {
      state: state === "qualified" ? "qualified" : "evidence-absent",
      reason: state === "qualified" ? "The exact execution identity is qualified." : "Live qualification is pending.",
      serving_lane_id: null,
      compared: ["model_revision", "runtime_image_digest", "runtime_kind"],
      mismatched: [],
    },
    execution_mode: seed.mode,
    batch_supported: true,
    interactive_supported: seed.interactive ?? seed.mode === "hybrid",
    service_classes: seed.mode === "hybrid"
      ? ["presentation", "interactive", "customer-batch", "bulk-backfill"]
      : ["customer-batch", "bulk-backfill"],
    backend: backend(seed.modelId, seed.repository, seed.character),
    access,
    caching: {
      exact_tier: seed.tier ?? "not-observed",
      image: state === "qualified" ? "verified" : "candidate",
      artifacts: state === "qualified" ? "verified" : "candidate",
      reference_data: seed.modelId.includes("alphafold") || seed.modelId.includes("protenix") ? "candidate" : "unsupported",
      runtime_checkpoint: "candidate",
      gpu_snapshot: "unsupported",
      reason: seed.tier
        ? `The exact observed tier is ${seed.tier}; GPU-memory snapshot restore is not claimed.`
        : "No exact fast-start tier has been observed for this runtime identity.",
    },
  };
}

export const scientificModelReadinessFixture: ScientificModelReadinessList = {
  items: [
    readiness({ modelId: "proteina-complexa", displayName: "Proteina-Complexa", repository: "Labs22/Proteina-Complexa", character: "8", mode: "scientific-batch" }),
    readiness({ modelId: "boltzgen", displayName: "BoltzGen", repository: "jwohlwend/boltzgen", character: "9", mode: "hybrid", interactive: true }),
    readiness({ modelId: "mosaic", displayName: "mosaic", repository: "mosaic-model/mosaic", character: "a", mode: "scientific-batch" }),
    readiness({ modelId: "bindcraft", displayName: "BindCraft (native PyRosetta)", repository: "martinpacesa/BindCraft", character: "b", mode: "scientific-batch", access: bindCraftAccess, tier: "container-image-local", interactive: true }),
    readiness({ modelId: "rfdiffusion", displayName: "RFdiffusion", repository: "RosettaCommons/RFdiffusion", character: "c", mode: "scientific-batch", readiness: "qualified", tier: "model-artifact-local" }),
    readiness({ modelId: "esmfold2-fast", displayName: "ESMFold2 / Fast", repository: "facebookresearch/esm", character: "d", mode: "hybrid", interactive: true }),
    readiness({ modelId: "protenix-v2", displayName: "Protenix v2", repository: "bytedance/Protenix", character: "e", mode: "scientific-batch" }),
    readiness({ modelId: "alphafold3", displayName: "AlphaFold3 (native)", repository: "google-deepmind/alphafold3", character: "f", mode: "scientific-batch", access: alphaFoldAccess, interactive: true }),
    readiness({ modelId: "openfold3", displayName: "OpenFold3 (explicit alternative)", repository: "aqlaboratory/openfold-3", character: "0", mode: "hybrid", interactive: true }),
  ],
  projection_issues: [],
};

export const scientificCapabilitiesFixture: ScientificCapabilities = {
  model_readiness: { available: true, reason: null },
  run_history: { available: true, reason: null },
  artifacts: { available: true, reason: null },
};
