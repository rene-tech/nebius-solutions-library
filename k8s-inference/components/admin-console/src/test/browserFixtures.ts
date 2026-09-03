import { academicAssetReadinessFixture } from "./academicFixtures.ts";
import { capacityFixture, observabilityFixture } from "./capacityObservabilityFixtures.ts";
import {
  awaitingStatus,
  completedStatus,
  configurationPlan,
  configurationRevision,
} from "./configurationFixtures.ts";
import {
  modelDeploymentAppliedFixture,
  modelDeploymentMutationCapabilitiesFixture,
  modelDeploymentPlanFixture,
  modelDeploymentRevisionFixture,
  modelDeploymentStatusFixture,
  modelDeploymentValidationFixture,
} from "./modelDeploymentFixtures.ts";
import {
  scientificCapabilitiesFixture,
  scientificModelReadinessFixture,
  scientificRunDetailFixture,
  scientificRunListFixture,
} from "./scientificFixtures.ts";

const now = "2026-08-30T08:30:00Z";
const context = {
  project: "project-fixture",
  cluster: "cluster-fixture",
  region: "us-north1",
  from_at: "2026-08-30T07:30:00Z",
  to_at: now,
  timezone: "UTC",
};

const measurement = (value: number | null, unit: string, state = value === null ? "unavailable" : "available", reason: string | null = value === null ? "not_instrumented" : null) => ({ value, unit, state, source: "fixture", reason });
const latency = {
  p50_seconds: measurement(0.21, "seconds"),
  p95_seconds: measurement(0.74, "seconds"),
  p99_seconds: measurement(1.42, "seconds"),
  ttft_p95_seconds: measurement(null, "seconds", "unavailable", "first-token timestamps are not instrumented"),
};
const modelMetrics = {
  terminal_operations: measurement(18432, "count"),
  requests_per_second: measurement(18.7, "requests/second"),
  error_operations: measurement(21, "count"),
  error_rate: measurement(0.0011, "ratio"),
  estimated_gpu_seconds: measurement(8741, "gpu-seconds", "estimated", "derived from replica allocation"),
  measured_gpu_seconds: measurement(null, "gpu-seconds", "unavailable", "DCGM series are not ingested"),
  tokens_per_second: measurement(null, "tokens/second", "unavailable", "token counters are not instrumented"),
  latency,
  cold_start_seconds: measurement(52.4, "seconds"),
};

const retainedQualification = {
  kind: "reviewed-evidence-snapshot" as const,
  authority: "reviewed-retained-evidence",
  observed_at: "2026-08-30T08:00:00Z",
  states: {
    registered: true,
    route_active: true,
    runtime_ready: true,
    semantic_qualified: true,
    http_mcp_qualified: true,
    cold_start_qualified: true,
    elasticity_qualified: false,
  },
};

const models = [
  {
    identity: { id: "qwen3-8b", display_name: "Qwen3 8B", family: "Text generation", support_state: "supported", enabled: true, model_revision: "fixture-revision", runtime_kind: "vLLM", runtime_image_digest: "sha256:fixture", gpu_class: "accelerator-80gb", gpu_count: 1, execution_mode: "single-node", protocols: ["openai-chat", "mcp"], public_endpoints: { chat: "/v1/chat/completions" }, mcp_exposed: true, mcp_tool_name: "infer_qwen3_8b", active_runtime: { variant_id: null, kind: "independent-runtime", source_kind: "huggingface", repository: "Qwen/Qwen3-8B", relationship: "canonical-runtime", nim_artifact_parity: "not-applicable" }, qualification: retainedQualification, policy: { license_id: "apache-2.0", non_clinical: false, commercial_use: "allowed" } },
    runtime: { state: "hot", reason: "semantic health is passing", activation_phase: "ready", desired_replicas: 2, ready_replicas: 2, queued_operations: 0, semantic_healthy: true, observed_at: now },
    metrics: modelMetrics,
  },
  {
    identity: { id: "glm-5-2-fp8", display_name: "GLM 5.2 FP8", family: "Text generation", support_state: "supported", enabled: true, model_revision: "fixture-revision", runtime_kind: "vLLM distributed", runtime_image_digest: "sha256:fixture", gpu_class: "accelerator-288gb", gpu_count: 8, execution_mode: "tensor-parallel", protocols: ["openai-chat", "mcp"], public_endpoints: { chat: "/v1/chat/completions" }, mcp_exposed: true, mcp_tool_name: "infer_glm_5_2", active_runtime: { variant_id: null, kind: "independent-runtime", source_kind: "huggingface", repository: "zai-org/GLM-5.2-FP8", relationship: "canonical-runtime", nim_artifact_parity: "not-applicable" }, qualification: retainedQualification, policy: { license_id: "mit", non_clinical: false, commercial_use: "allowed" } },
    runtime: { state: "loading", reason: "activation is restoring a snapshot", activation_phase: "claimed", desired_replicas: 1, ready_replicas: 0, queued_operations: 3, semantic_healthy: null, observed_at: now },
    metrics: { ...modelMetrics, requests_per_second: measurement(0, "requests/second"), cold_start_seconds: measurement(84.3, "seconds", "estimated", "critical-path projection") },
  },
  {
    identity: { id: "nv-segment-ct", display_name: "NVIDIA CT Segmentation", family: "Medical imaging", support_state: "supported", enabled: true, model_revision: "fixture-revision", runtime_kind: "custom MONAI", runtime_image_digest: "sha256:fixture", gpu_class: "accelerator-any", gpu_count: 1, execution_mode: "single-node", protocols: ["http"], public_endpoints: { infer: "/models/nv-segment-ct/infer" }, mcp_exposed: true, mcp_tool_name: "infer_nv_segment_ct", active_runtime: { variant_id: "nv-segment-ct-upstream-blackwell-sm103", kind: "independent-runtime", source_kind: "huggingface", repository: "nvidia/NV-Segment-CT", relationship: "exact-model", nim_artifact_parity: "unverified" }, qualification: retainedQualification, policy: { license_id: "NVIDIA-Open-Model-License", non_clinical: true, commercial_use: "license-dependent" } },
    runtime: { state: "cold", reason: "supported with no active or queued demand", activation_phase: "none", desired_replicas: 0, ready_replicas: 0, queued_operations: 0, semantic_healthy: null, observed_at: now },
    metrics: { ...modelMetrics, requests_per_second: measurement(0, "requests/second"), cold_start_seconds: measurement(127.8, "seconds") },
  },
];

const operation = {
  id: "10f61fc4-4211-4bb8-a058-b11a8c078520",
  tenant_id: "tenant-fixture",
  principal_id: "team-research",
  api_key_prefix: "fs2_live_7A3C",
  model_id: "qwen3-8b",
  model_revision: "fixture-revision",
  protocol: "openai-chat",
  operation: "chat.completion",
  status: "succeeded",
  accepted_at: "2026-08-30T08:29:55Z",
  completed_at: "2026-08-30T08:29:56Z",
  outcome: "success",
  semantic_outcome: "pass",
  http_status: 200,
  error_class: null,
  attempt: 1,
  max_attempts: 3,
  gpu_count: 1,
  preemptible: true,
  estimated_gpu_seconds: measurement(1.1, "gpu-seconds", "estimated", "allocation-time estimate"),
  input_tokens: measurement(null, "count", "unavailable", "token counters are not instrumented"),
  output_tokens: measurement(null, "count", "unavailable", "token counters are not instrumented"),
  timings: { queue_seconds: measurement(0.02, "seconds"), cold_start_seconds: measurement(0, "seconds"), inference_seconds: measurement(0.71, "seconds"), total_seconds: measurement(0.76, "seconds"), ttft_seconds: measurement(null, "seconds", "unavailable", "first-token timestamps are not instrumented") },
};

const operatorPrincipal = {
  id: "00000000-0000-0000-0000-000000000001",
  subject: "bootstrap-admin",
  display_name: "Platform administrator",
  kind: "human",
  role: "admin",
  tenant_id: null,
  enabled: true,
  created_at: "2026-08-30T06:00:00Z",
  created_by: "bootstrap",
  updated_at: "2026-08-30T06:00:00Z",
  disabled_at: null,
};

const servicePrincipal = {
  id: "f31f9054-90ca-4ac2-aa13-f23d38ca2c0f",
  subject: "research-agent",
  display_name: "Research agent",
  kind: "service",
  role: "operator",
  tenant_id: "tenant-fixture",
  enabled: true,
  created_at: "2026-08-30T06:30:00Z",
  created_by: "bootstrap-admin",
  updated_at: "2026-08-30T07:10:00Z",
  disabled_at: null,
};

const accessMeasurement = (value: number | null, unit: string, state = value === null ? "unavailable" : "available", reason: string | null = value === null ? "runtime reporting is incomplete" : null) => ({ value, unit, state, reason });
const fixtureKey = {
  id: "21d54dd4-931e-4988-95d2-eef0ead8bd40",
  name: "Research agent production",
  prefix: "fs2_pat_21d54dd4931e",
  fingerprint: "a58974af8e91b3f99c8bf77721bfc39945c61b79d8cdb5520333df395bf7861f",
  principal_id: "research-agent",
  tenant_id: "tenant-fixture",
  scopes: ["inference.invoke", "mcp.invoke", "operations.read"],
  models: ["qwen3-8b", "glm-5-2-fp8"],
  state: "active",
  expires_at: "2026-09-30T00:00:00Z",
  last_used_at: "2026-08-30T08:29:56Z",
  request_budget: 100000,
  requests_used: 18432,
  gpu_seconds_budget: 250000,
  gpu_seconds_used: 8741,
  gpu_seconds_reserved: 8,
  max_concurrency: 8,
  rate_limit_requests: 120,
  rate_window_seconds: 60,
  rate_window_started_at: "2026-08-30T08:29:00Z",
  rate_window_requests: 19,
  rotation_parent_id: null,
  rotated_at: null,
  created_at: "2026-08-30T06:40:00Z",
  created_by: "bootstrap-admin",
  revoked_at: null,
  usage: {
    terminal_operations: 18432,
    estimated_gpu_seconds: accessMeasurement(8741, "gpu-seconds", "estimated", "admission reservation accounting"),
    input_tokens: accessMeasurement(932441, "tokens"),
    output_tokens: accessMeasurement(486021, "tokens"),
    token_reported_operations: 18432,
    modality_reported_operations: 18432,
    modality_units: [{ modality: "image", direction: "input", unit: "image", amount: 113 }],
    modality_state: "available",
    modality_reason: null,
  },
};

const auditEvents = [
  { id: 41, occurred_at: "2026-08-30T08:29:56Z", actor: "research-agent", tenant_id: "tenant-fixture", token_id: fixtureKey.id, action: "token.use", target_type: "token", target_id: fixtureKey.id, outcome: "succeeded", detail: { model_id: "qwen3-8b" } },
  { id: 40, occurred_at: "2026-08-30T08:12:03Z", actor: "bootstrap-admin", tenant_id: "tenant-fixture", token_id: fixtureKey.id, action: "token.policy.update", target_type: "token", target_id: fixtureKey.id, outcome: "succeeded", detail: { max_concurrency: 8 } },
];

function envelope(data: unknown) {
  return { meta: { schema_version: "fs2.admin-api/v1", generated_at: now, context, sources: [{ id: "fixture", state: "available", observed_at: now, age_seconds: 0, reason: null }], warnings: [] }, data };
}

export function browserFixture(path: string): unknown | undefined {
  if (path === "/admin/api/v1/session") return envelope({ id: "34718a7e-29a9-4677-a256-481897956cf8", principal: operatorPrincipal, created_at: "2026-08-30T08:00:00Z", expires_at: "2026-08-30T16:00:00Z", last_seen_at: now, revoked_at: null });
  if (path === "/admin/api/v1/principals") return envelope({ items: [operatorPrincipal, servicePrincipal] });
  if (path === "/admin/api/v1/keys") return envelope({ items: [fixtureKey] });
  if (path === "/admin/api/v1/audit") return envelope({ items: auditEvents });
  if (path === "/admin/api/v1/context") return envelope({ selected: context, options: [{ project: context.project, cluster: context.cluster, region: context.region, label: "Inference Platform · cluster-fixture · us-north1" }], server_authoritative: true });
  if (path === "/admin/api/v1/overview") return envelope({ model_states: [{ state: "hot", models: 1 }, { state: "loading", models: 1 }, { state: "cold", models: 1 }, { state: "queued", models: 0 }, { state: "unhealthy", models: 0 }, { state: "unsupported", models: 0 }, { state: "unknown", models: 0 }], requests_per_second: measurement(18.7, "requests/second"), tokens_per_second: measurement(null, "tokens/second", "unavailable", "token counters are not instrumented"), terminal_operations: measurement(18432, "count"), error_operations: measurement(21, "count"), error_rate: measurement(0.0011, "ratio"), estimated_gpu_seconds: measurement(8741, "gpu-seconds", "estimated", "derived from allocation"), measured_gpu_seconds: measurement(null, "gpu-seconds", "unavailable", "DCGM series are not ingested"), queued_operations: measurement(3, "count"), oldest_queue_age_seconds: measurement(8.2, "seconds"), latency, capacity: { allocatable_gpus: measurement(38, "count"), ready_gpu_nodes: measurement(10, "count"), preemptible_gpu_nodes: measurement(10, "count"), active_gpu_replicas: measurement(13, "count") }, reconciliation: { durable_terminal_operations: measurement(18432, "count"), prometheus_terminal_operations: measurement(18432, "count"), difference: measurement(0, "count") } });
  if (path === "/admin/api/v1/models") return envelope({ items: models, total: models.length });
  if (path.startsWith("/admin/api/v1/models/")) { const id = decodeURIComponent(path.slice("/admin/api/v1/models/".length)); const model = models.find((item) => item.identity.id === id); return model ? envelope({ model, snapshot_restore_seconds: measurement(35.4, "seconds"), cache_residency_bytes: measurement(null, "bytes", "unavailable", "cache byte telemetry is not instrumented"), cold_start_phase_breakdown: measurement(null, "seconds", "unavailable", "phase breakdown is not yet published") }) : undefined; }
  if (path === "/admin/api/v1/operations") return envelope({ items: [operation], next_cursor: null });
  if (path.startsWith("/admin/api/v1/operations/")) return envelope({ operation, payloads_exposed: false });
  if (path === "/admin/api/v1/scientific-capabilities") return envelope(scientificCapabilitiesFixture);
  if (path === "/admin/api/v1/scientific-runs") return envelope(scientificRunListFixture);
  if (path.startsWith("/admin/api/v1/scientific-runs/")) return envelope(scientificRunDetailFixture);
  if (path === "/admin/api/v1/scientific-models") return envelope(scientificModelReadinessFixture);
  if (path === "/admin/api/v1/academic-assets") return envelope(academicAssetReadinessFixture);
  if (path === "/admin/api/v1/capacity") return envelope(capacityFixture);
  if (path === "/admin/api/v1/observability") return envelope(observabilityFixture);
  if (path === "/admin/api/v1/configuration") return envelope(configurationRevision);
  if (path === "/admin/api/v1/configuration:diff") return envelope(configurationPlan().diff);
  if (path === "/admin/api/v1/configuration:validate") return envelope(configurationPlan().validation);
  if (path === "/admin/api/v1/configuration:plan") return envelope(configurationPlan());
  if (path === "/admin/api/v1/configuration:reconcile") return envelope(awaitingStatus);
  if (path.startsWith("/admin/api/v1/configuration/reconciliations/")) return envelope(completedStatus);
  if (path === "/admin/api/v1/configuration:rollback") return envelope({ target_revision: 1, plan: configurationPlan() });
  if (path === "/admin/api/v1/model-deployments") return envelope({ items: [modelDeploymentRevisionFixture], next_after: null });
  if (path === "/admin/api/v1/model-deployments:capabilities") return envelope(modelDeploymentMutationCapabilitiesFixture);
  if (path === "/admin/api/v1/model-deployments:validate-preview") return envelope(modelDeploymentValidationFixture);
  if (path === "/admin/api/v1/model-deployments:plan-preview") return envelope(modelDeploymentPlanFixture);
  if (path === "/admin/api/v1/model-deployments:apply") return envelope(modelDeploymentAppliedFixture);
  if (path === "/admin/api/v1/model-deployments/qwen-live:drain") return envelope(modelDeploymentAppliedFixture);
  if (path === "/admin/api/v1/model-deployments/qwen-live:rollback") return envelope(modelDeploymentAppliedFixture);
  if (path === "/admin/api/v1/model-deployments/qwen-live:reconcile") return envelope(modelDeploymentAppliedFixture);
  if (path === "/admin/api/v1/model-deployments/qwen-live/history") return envelope({ items: [modelDeploymentRevisionFixture, { ...modelDeploymentRevisionFixture, revision: 1, action: "create", previous_revision: null, created_at: "2026-09-02T07:00:00Z" }], next_before_revision: null });
  if (path === "/admin/api/v1/model-deployments/qwen-live/status") return envelope(modelDeploymentStatusFixture);
  if (path === "/admin/api/v1/model-deployments/qwen-live") return envelope(modelDeploymentRevisionFixture);
  return undefined;
}
