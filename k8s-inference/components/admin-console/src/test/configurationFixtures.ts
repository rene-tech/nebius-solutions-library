import type {
  ConfigurationPlan,
  ConfigurationRevision,
  PlatformConfiguration,
  ReconciliationStatus,
  TerraformHandoff,
} from "../api/configurationTypes.ts";

const hash = (character: string) => character.repeat(64);

export const configuration: PlatformConfiguration = {
  schema_version: "fs2.admin-configuration/v1",
  pools: {
    "elastic-b300": {
      resource_name: "nvidia.com/gpu",
      accelerator_class: "nvidia-b300-sxm",
      capacity_type: "preemptible",
      accelerators_per_node: 8,
      min_nodes: 0,
      max_nodes: 12,
      node_selector: { "accelerator.fs2.example/class": "b300" },
      tolerations: [{ key: "nvidia.com/gpu", operator: "Exists", value: null, effect: "NoSchedule", toleration_seconds: null }],
    },
  },
  models: {
    "qwen3-8b": {
      model_id: "qwen3-8b",
      enabled: true,
      placement: { pool_ids: ["elastic-b300"], accelerators: 1, topology_policy: "single-node" },
      autoscaling: { min_replicas: 0, max_replicas: 2, target_queue_depth: 1, polling_interval_seconds: 5, cooldown_seconds: 300 },
      queue: { local_queue: "inference", priority_class: "interactive", max_queue_seconds: 7200 },
      snapshot: { strategy: "weights", cache_tier: "node-local", restore_timeout_seconds: 600, parallelism: 4, require_semantic_check: true },
      mcp: { exposed: true, tool_name: "qwen3_8b" },
      rate: { requests_per_minute: 120, concurrent_requests: 4, accelerator_seconds_per_day: null },
      artifact: {
        image_repository: "registry.example.test/fs2/qwen",
        image_digest: `sha256:${hash("1")}`,
        model_revision: "4f5b1a",
        artifact_manifest_sha256: hash("2"),
        acquisition_contract_sha256: hash("3"),
        provenance_sha256: hash("4"),
        semantic_health_contract_sha256: hash("5"),
      },
    },
  },
};

export const configurationRevision: ConfigurationRevision = {
  revision: 2,
  etag: hash("a"),
  desired: configuration,
  effective: configuration,
  created_at: "2026-08-30T09:00:00Z",
  created_by: "terraform-applied",
  previous_revision: 1,
  reconciliation_id: null,
};

export function proposedConfiguration(cooldown = 301): PlatformConfiguration {
  const proposed = structuredClone(configuration);
  proposed.models["qwen3-8b"].autoscaling.cooldown_seconds = cooldown;
  return proposed;
}

const variables = {
  admin_configuration: proposedConfiguration(),
  admin_configuration_sha256: hash("b"),
  admin_configuration_plan_id: "11111111-1111-4111-8111-111111111111",
  admin_configuration_reconciliation_id: "11111111-1111-4111-8111-111111111111",
  admin_configuration_base_revision: 2,
  admin_configuration_base_etag: hash("a"),
  model_scaling_mode: "keda",
  hot_model_ids: [],
  model_scaling_overrides: {
    "qwen3-8b": { min_replicas: 0, max_replicas: 2, target_queue_depth: 1, polling_interval_seconds: 5, cooldown_seconds: 301 },
  },
  keda_cooldown_period_seconds: 301,
};

export const terraformHandoff: TerraformHandoff = {
  required: true,
  state: "review-required",
  variables,
  variables_sha256: hash("c"),
  expected_source_etag: hash("b"),
  tfvars_filename: "admin-configuration-11111111-1111-4111-8111-111111111111.tfvars.json",
  tfvars_json: JSON.stringify(variables, null, 2),
  tfvars_sha256: hash("d"),
  forbidden_browser_actions: ["terraform.apply", "cloud.mutate", "kubernetes.patch"],
};

export function configurationPlan(state: ConfigurationPlan["state"] = "valid"): ConfigurationPlan {
  const issue = state === "valid" ? [] : [{
    severity: "error" as const,
    code: "configuration_change_not_applicable",
    path: "$.models.qwen3-8b.snapshot.parallelism",
    message: "field is typed for review but has no proven consumer in the current Terraform root",
  }];
  return {
    plan_id: "11111111-1111-4111-8111-111111111111",
    state,
    base_revision: 2,
    base_etag: hash("a"),
    proposed: proposedConfiguration(),
    proposed_etag: hash("b"),
    validation: { valid: state === "valid", proposed_etag: hash("b"), issues: issue },
    diff: {
      base_revision: 2,
      base_etag: hash("a"),
      proposed_etag: hash("b"),
      changes: [{ path: "$.models.qwen3-8b.autoscaling.cooldown_seconds", owner: "terraform", before: 300, after: 301 }],
      runtime_change_count: 0,
      terraform_change_count: 1,
    },
    artifacts: state === "valid" ? [{ kind: "ModelConfiguration", name: "qwen3-8b", sha256: hash("e"), source: "catalog/models/qwen3-8b.json" }] : [],
    terraform: state === "valid" ? structuredClone(terraformHandoff) : {
      required: false,
      state: "not-required",
      variables: {},
      variables_sha256: hash("f"),
      expected_source_etag: hash("b"),
      tfvars_filename: "admin-configuration-11111111-1111-4111-8111-111111111111.tfvars.json",
      tfvars_json: "{}",
      tfvars_sha256: hash("0"),
      forbidden_browser_actions: ["terraform.apply", "cloud.mutate", "kubernetes.patch"],
    },
    created_at: "2026-08-30T09:02:00Z",
    expires_at: "2026-08-30T09:17:00Z",
    created_by: "operator@example.test",
  };
}

export const awaitingStatus: ReconciliationStatus = {
  reconciliation_id: "11111111-1111-4111-8111-111111111111",
  plan_id: "11111111-1111-4111-8111-111111111111",
  phase: "awaiting-terraform-plan-apply",
  base_revision: 2,
  target_etag: hash("b"),
  applied_revision: null,
  previous_revision: 2,
  artifact_sha256: [hash("e")],
  terraform_variables_sha256: hash("c"),
  started_at: "2026-08-30T09:03:00Z",
  completed_at: null,
  error_code: null,
};

export const completedStatus: ReconciliationStatus = {
  ...awaitingStatus,
  phase: "succeeded",
  applied_revision: 3,
  completed_at: "2026-08-30T09:05:00Z",
};
