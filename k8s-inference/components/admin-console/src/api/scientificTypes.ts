export type ScientificServiceClass =
  | "presentation"
  | "interactive"
  | "customer-batch"
  | "bulk-backfill";

export type ScientificAccessProfile = "standard" | "academic";
export type ScientificAccessState = "not-required" | "unverified" | "verified" | "blocked";
export type ScientificExecutionMode = "scientific-batch" | "hybrid";
export type ScientificRunState =
  | "waiting-for-access"
  | "queued"
  | "admitted"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled";
export type ScientificAdmissionState = "pending" | "inadmissible" | "admitted" | "evicted" | "finished";
export type ScientificStageState =
  | "pending"
  | "queued"
  | "admitted"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "skipped";
export type ScientificAttemptState = "queued" | "running" | "succeeded" | "failed" | "preempted" | "cancelled";
export type ScientificEvidenceState = "measured" | "estimated" | "unavailable";
export type ScientificFastStartTier =
  | "cold"
  | "container-image-local"
  | "model-artifact-local"
  | "runtime-checkpoint-restore"
  | "gpu-memory-snapshot-restore"
  | "warm-replica"
  | "not-observed";

export interface ScientificEvidenceMeasurement {
  value: number | null;
  unit: "seconds" | "gpu-seconds" | "bytes" | "count";
  evidence: ScientificEvidenceState;
  source: string;
  reason: string | null;
}

export interface ScientificAccessGate {
  profile: ScientificAccessProfile;
  state: ScientificAccessState;
  gate: string;
  receipt_digest: string | null;
  request_time_license_receipt_required: false;
  authorization: {
    asset_id: string;
    backend_id: string | null;
    license_id: string | null;
    use_authorization_status: "Granted";
    execution_authorization_status: "Authorized";
    serving_admission: "PendingRuntimeReadiness" | "AdmittedNoPerRequestLicenseReceipt";
    asset_namespace: string | null;
  } | null;
  formal_license_status: "FormalAcceptancePending" | "FormalAcceptanceRecorded" | "not-applicable";
  credentials_exposed: false;
  alternative: {
    model_id: string;
    display_name: string;
    relationship: "explicit-alternative";
    reason: string;
  } | null;
}

export interface ScientificBackendIdentity {
  backend_id: string;
  kind: string;
  source_repository: string;
  source_revision: string | null;
  model_revision: string | null;
  runtime_image_digest: string | null;
  execution_identity_digest: string | null;
}

export interface ScientificQueueState {
  tenant_queue: string;
  model_lane: string;
  local_queue: string;
  cluster_queue: string;
  workload_priority_class: string;
  priority_value: number;
  admission_state: ScientificAdmissionState;
  admission_reason: string;
  admitted_at: string | null;
  queue_position: ScientificEvidenceMeasurement;
}

export interface ScientificServiceClassDecision {
  requested: ScientificServiceClass;
  effective: ScientificServiceClass;
  reason: string;
  policy_revision: string;
}

export interface ScientificFastStartObservation {
  tier: ScientificFastStartTier;
  evidence: "observed" | "declared" | "unavailable";
  observed_at: string | null;
  runtime_identity_digest: string | null;
  reason: string;
}

export interface ScientificLifecyclePhase {
  phase:
    | "queue"
    | "admission"
    | "image-pull"
    | "artifact-load"
    | "restore"
    | "semantic-warmup"
    | "active-compute"
    | "allocated-idle"
    | "grace-drain"
    | "teardown";
  duration: ScientificEvidenceMeasurement;
}

export interface ScientificGpuAccounting {
  gpu_count: number | null;
  capacity_type: "regular" | "preemptible" | "capacity-block" | "unknown";
  allocated: ScientificEvidenceMeasurement;
  active: ScientificEvidenceMeasurement;
  idle_total: ScientificEvidenceMeasurement;
  idle_by_cause: Array<{
    cause: "image-pull" | "artifact-load" | "restore" | "warmup" | "between-stages" | "scheduler-hold" | "unattributed";
    duration: ScientificEvidenceMeasurement;
  }>;
  grace_drain: ScientificEvidenceMeasurement;
  reconciliation_delta: ScientificEvidenceMeasurement;
}

export interface ScientificError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface ScientificAttempt {
  id: string;
  number: number;
  status: ScientificAttemptState;
  started_at: string | null;
  completed_at: string | null;
  workload_uid: string | null;
  job_uid: string | null;
  pod_count: number | null;
  node_count: number | null;
  gpu_count: number | null;
  admitted_at: string | null;
  resolved_pool_id: string | null;
  admitted_resource_flavor: string | null;
  accelerator_resource_name: string | null;
  checkpoint_input_artifact_id: string | null;
  checkpoint_output_artifact_id: string | null;
  error: ScientificError | null;
}

export interface ScientificStage {
  id: string;
  display_name: string;
  ordinal: number;
  needs: string[];
  resource_class: "cpu" | "gpu";
  admission_mode: "independent-jobs" | "gang-jobset";
  checkpoint_mode: "none" | "restart" | "resume";
  status: ScientificStageState;
  attempts: ScientificAttempt[];
}

export interface ScientificArtifact {
  artifact_id: string;
  name: string;
  role: "input" | "checkpoint" | "output" | "validation" | "manifest";
  semantic_type: string;
  state: "available" | "pending" | "failed" | "expired";
  sha256: string | null;
  size_bytes: ScientificEvidenceMeasurement;
  media_type: string;
  created_at: string | null;
  download: {
    available: boolean;
    href: string | null;
    reason: string | null;
  };
}

export interface ScientificObservabilityLink {
  kind: "trace" | "logs" | "metrics";
  label: string;
  available: boolean;
  href: string | null;
  reason: string | null;
}

export interface ScientificRunSummary {
  id: string;
  batch_id: string;
  display_name: string;
  operation: string;
  status: ScientificRunState;
  submitted_at: string;
  completed_at: string | null;
  attribution: {
    tenant_id: string;
    user_id: string;
    principal_id: string;
    api_key_prefix: string;
  };
  model: {
    model_id: string;
    display_name: string;
    execution_mode: ScientificExecutionMode;
    backend: ScientificBackendIdentity;
  };
  access: ScientificAccessGate;
  service_class: ScientificServiceClassDecision;
  queue: ScientificQueueState;
  fast_start: ScientificFastStartObservation;
  stage_counts: Record<ScientificStageState, number>;
  gpu_accounting: ScientificGpuAccounting;
  error: ScientificError | null;
  cancellation: {
    state: "not-requested" | "requested" | "acknowledged" | "denied";
    requested_at: string | null;
    requested_by: string | null;
    reason: string | null;
    mode: "terminate-attempt" | "checkpoint-then-terminate";
    grace_seconds: number | null;
    can_cancel: boolean;
  };
}

export interface ScientificRunDetail {
  run: ScientificRunSummary;
  lifecycle_phases: ScientificLifecyclePhase[];
  stages: ScientificStage[];
  artifacts: ScientificArtifact[];
  retry: {
    max_attempts_per_stage: number;
    retryable_exit_codes: number[];
  };
  semantic_validation: {
    validator_id: string;
    status: "passed" | "failed" | "not-run";
    receipt_digest: string | null;
  };
  observability: ScientificObservabilityLink[];
  payloads_exposed: false;
}

export interface ScientificRunList {
  items: ScientificRunSummary[];
  next_cursor: string | null;
}

export interface ScientificCachingReadiness {
  exact_tier: ScientificFastStartTier;
  image: "verified" | "candidate" | "unsupported" | "unavailable";
  artifacts: "verified" | "candidate" | "unsupported" | "unavailable";
  reference_data: "verified" | "candidate" | "unsupported" | "unavailable";
  runtime_checkpoint: "verified" | "candidate" | "unsupported" | "unavailable";
  gpu_snapshot: "verified" | "candidate" | "unsupported" | "unavailable";
  reason: string;
}

export interface ScientificModelReadiness {
  model_id: string;
  candidate_id: string;
  display_name: string;
  readiness: "qualified" | "candidate" | "blocked" | "unknown";
  readiness_reason: string;
  workload_profile: "published" | "absent";
  missing_evidence: string[];
  qualification: {
    state: "qualified" | "identity-mismatch" | "not-registered" | "evidence-absent";
    reason: string;
    serving_lane_id: string | null;
    compared: string[];
    mismatched: string[];
  };
  execution_mode: ScientificExecutionMode | null;
  batch_supported: boolean;
  interactive_supported: boolean | null;
  service_classes: ScientificServiceClass[];
  backend: ScientificBackendIdentity;
  access: ScientificAccessGate;
  caching: ScientificCachingReadiness;
}

export interface ScientificModelReadinessList {
  items: ScientificModelReadiness[];
  projection_issues: Array<{
    candidate_id: string | null;
    source: "candidate-receipt" | "workload-profile" | "academic-readiness" | "variant-map";
    reason: string;
  }>;
}

export interface ScientificCapability {
  available: boolean;
  reason: string | null;
}

export interface ScientificCapabilities {
  model_readiness: ScientificCapability;
  run_history: ScientificCapability;
  artifacts: ScientificCapability;
}

export type AcademicAssetState =
  | "UseAuthorizationMissing" | "MissingArtifact" | "MissingTenantCache"
  | "MissingRuntimeValidation" | "MissingDeployment" | "MissingSemanticReadiness"
  | "ImageRebuildPending" | "Ready" | "InvalidContract" | "InvalidTenantCacheReceipt"
  | "InvalidRuntimeReceipt" | "InvalidDeploymentReceipt" | "InvalidSemanticReceipt";

export interface AcademicAssetReadiness {
  asset_id: string;
  model_id: string;
  backend_id: string;
  display_name: string;
  state: AcademicAssetState;
  serving_admission: "PendingRuntimeReadiness" | "AdmittedNoPerRequestLicenseReceipt";
  use_authorization_status: "Missing" | "Granted";
  execution_authorization_status: "NotAuthorized" | "Authorized";
  formal_license_status: "FormalAcceptancePending" | "FormalAcceptanceRecorded";
  artifact_status: "MissingArtifact" | "ArtifactVerified";
  tenant_cache_status: "MissingTenantCache" | "TenantCacheReady" | "InvalidEvidence";
  runtime_status:
    | "MissingRuntimeValidation"
    | "RuntimeReady"
    | "RuntimeSemanticPassedImageRebuildPending"
    | "InvalidEvidence";
  deployment_status: "MissingDeployment" | "DeploymentReady" | "InvalidEvidence";
  semantic_status: "MissingSemanticReadiness" | "SemanticReady" | "InvalidEvidence";
  delivery_mode: "tenant-private-volume";
  embed_in_image: false;
  mount_path: string;
  artifact_sha256: string | null;
  runtime_image_digest: string | null;
  runtime_environment_digest: string | null;
  authorization_receipt_sha256: string | null;
  acceptance_receipt_sha256: string | null;
  alternative: { model_id: string; reason: string } | null;
}

export interface AcademicAssetReadinessList {
  tenant_id: string;
  institution_id: string | null;
  runtime_path_state: "Blocked" | "Ready";
  formal_license_state: "Pending" | "Recorded";
  request_time_license_receipt_required: false;
  delivery: { namespace: string; claim: string; mount_root: string; general_shared_cache: false };
  items: AcademicAssetReadiness[];
}
