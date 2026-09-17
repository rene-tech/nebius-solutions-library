# Repeat the provider/Kubernetes authority join during the workloads apply.
# This gate is ordered after every control-plane prerequisite and immediately
# before Helm, so neither the foundation receipt nor plan-time Node data can be
# replayed after a cordon, taint, replacement, provider rollout, or group move.
locals {
  public_edge_gate_launcher_path = "/usr/local/libexec/fs2-public-edge-current/launcher"
  public_edge_gate_verifier_path = "${path.module}/../foundation/scripts/verify-public-edge-node-eligibility.py"
  public_edge_gate_verifier_sha256 = filesha256(local.public_edge_gate_verifier_path)
  public_edge_membership_trust_sha256 = filesha256("${path.module}/../foundation/trusted-public-edge-membership-issuers.json")
  public_edge_provider_adapter_trust_sha256 = filesha256("${path.module}/../foundation/trusted-public-edge-provider-adapters.json")
}

resource "terraform_data" "public_edge_apply_eligibility" {
  count = local.public_edge_enabled ? 1 : 0

  input = {
    schema                    = "fs2-serve.nebius.ai/public-edge-apply-eligibility/v1"
    stage                     = "workloads"
    planned_at                = plantimestamp()
    maximum_plan_age_seconds  = 14400
    cluster_id                = var.cluster_id
    kube_system_uid           = var.kube_system_uid
    system_node_group_id      = var.public_edge_availability_contract.system_node_group_id
    expected_node_count       = var.public_edge_availability_contract.system_node_count
    minimum_hostname_domains  = var.public_edge_availability_contract.minimum_domains
    maximum_surge_members     = var.public_edge_availability_contract.update_strategy.max_surge
    node_selector_sha256      = sha256(jsonencode(var.public_edge_availability_contract.node_selector))
    verifier_sha256           = local.public_edge_gate_verifier_sha256
  }

  triggers_replace = {
    planned_at               = plantimestamp()
    cluster_id               = var.cluster_id
    kube_system_uid          = var.kube_system_uid
    system_node_group_id     = var.public_edge_availability_contract.system_node_group_id
    availability_contract_sha256 = local.public_edge_availability_contract_sha256
    verifier_sha256          = local.public_edge_gate_verifier_sha256
  }

  provisioner "local-exec" {
    command     = "local-exec"
    interpreter = [local.public_edge_gate_launcher_path, "public-edge-verifier"]
    quiet       = true

    environment = {
      FS2_EDGE_GATE_STAGE                  = self.input.stage
      FS2_EDGE_GATE_PLANNED_AT             = self.input.planned_at
      FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS   = tostring(self.input.maximum_plan_age_seconds)
      FS2_EDGE_GATE_RUN_ROOT               = abspath(var.run_root)
      FS2_EDGE_GATE_KUBECONFIG             = abspath(var.kubeconfig_path)
      FS2_EDGE_GATE_KUBE_CONTEXT           = var.kube_context
      FS2_EDGE_GATE_CLUSTER_ID             = self.input.cluster_id
      FS2_EDGE_GATE_PROJECT_ID             = nonsensitive(var.project_id)
      FS2_EDGE_GATE_KUBE_SYSTEM_UID        = self.input.kube_system_uid
      FS2_EDGE_GATE_NODE_GROUP_ID          = self.input.system_node_group_id
      FS2_EDGE_GATE_RUN_ID                 = var.run_id
      FS2_EDGE_GATE_EXPECTED_NODE_COUNT    = tostring(self.input.expected_node_count)
      FS2_EDGE_GATE_MINIMUM_DOMAINS        = tostring(self.input.minimum_hostname_domains)
      FS2_EDGE_GATE_MAXIMUM_SURGE_MEMBERS  = tostring(self.input.maximum_surge_members)
      FS2_EDGE_GATE_NODE_SELECTOR_JSON     = jsonencode(var.public_edge_availability_contract.node_selector)
      FS2_EDGE_GATE_VERIFIER_SHA256        = self.input.verifier_sha256
      FS2_EDGE_GATE_POLICY_SHA256          = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_policy_sha256
      FS2_EDGE_GATE_BINDING_SHA256         = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_binding_sha256
      FS2_EDGE_GATE_CAS_POLICY_SHA256      = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_cas_policy_sha256
      FS2_EDGE_GATE_CAS_BINDING_SHA256     = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_cas_binding_sha256
      FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256  = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_bootstrap_policy_sha256
      FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256 = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_bootstrap_binding_sha256
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.api_version
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND        = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.kind
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME        = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.name
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE    = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.resource
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256      = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.sha256
      FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256 = local.public_edge_membership_trust_sha256
      FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256 = local.public_edge_provider_adapter_trust_sha256
    }
  }

  depends_on = [
    kubernetes_manifest.model_deployment_crd,
    kubernetes_manifest.control_database,
    kubernetes_secret_v1.database_consumer,
    kubernetes_secret_v1.grafana_datasource,
    kubernetes_secret_v1.payload_keyring,
    kubernetes_secret_v1.ledger_keyring,
    kubernetes_secret_v1.token_pepper,
    kubernetes_secret_v1.route_attestors,
    kubernetes_secret_v1.admin,
    kubernetes_secret_v1.bootstrap_access,
    kubernetes_secret_v1.scientific_access,
    kubernetes_secret_v1.scientific_artifact_store,
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache,
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache_additional,
    kubernetes_job_v1.scientific_runtime_cache_bootstrap,
    kubernetes_job_v1.scientific_runtime_cache_bootstrap_additional,
    terraform_data.scientific_artifacts_contract,
    kubernetes_config_map_v1.serving_bindings,
    kubernetes_config_map_v1.lean_routes,
    kubernetes_config_map_v1.admin_configuration,
    kubernetes_config_map_v1.model_controller_envelope,
    kubernetes_config_map_v1.model_controller_bundles,
    kubernetes_persistent_volume_claim_v1.fast_start_compile_cache,
    kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt,
    kubernetes_persistent_volume_claim_v1.scientific_snapshots,
    kubernetes_config_map_v1.scientific_snapshot_sources,
    kubernetes_persistent_volume_claim_v1.serving_snapshots,
    kubernetes_config_map_v1.serving_snapshot_sources,
    kubernetes_config_map_v1.scientific_scheduling_contract,
    kubernetes_manifest.model,
    module.academic_assets,
    module.reference_data,
    kubernetes_manifest.additional_local_queue,
    kubernetes_manifest.general_cpu_local_queue,
    kubernetes_manifest.model_local_queue,
  ]
}

# This deferred external read is consumed by helm_release.control_plane's own
# lifecycle precondition. It is therefore evaluated after all chart
# prerequisites and in the protected Helm mutation path, rather than being a
# reusable plan-time or foundation receipt.
data "external" "public_edge_mutation_fence" {
  count = local.public_edge_enabled ? 1 : 0

  program = [
    local.public_edge_gate_launcher_path,
    "public-edge-verifier",
    "external",
  ]

  query = {
    gate_id                                  = terraform_data.public_edge_apply_eligibility[0].id
    verifier_sha256                          = local.public_edge_gate_verifier_sha256
    FS2_EDGE_GATE_STAGE                       = "workloads-mutation"
    FS2_EDGE_GATE_PLANNED_AT                  = terraform_data.public_edge_apply_eligibility[0].output.planned_at
    FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS        = tostring(terraform_data.public_edge_apply_eligibility[0].output.maximum_plan_age_seconds)
    FS2_EDGE_GATE_RUN_ROOT                    = abspath(var.run_root)
    FS2_EDGE_GATE_KUBECONFIG                  = abspath(var.kubeconfig_path)
    FS2_EDGE_GATE_KUBE_CONTEXT                = var.kube_context
    FS2_EDGE_GATE_CLUSTER_ID                  = terraform_data.public_edge_apply_eligibility[0].output.cluster_id
    FS2_EDGE_GATE_PROJECT_ID                  = nonsensitive(var.project_id)
    FS2_EDGE_GATE_KUBE_SYSTEM_UID             = terraform_data.public_edge_apply_eligibility[0].output.kube_system_uid
    FS2_EDGE_GATE_NODE_GROUP_ID               = terraform_data.public_edge_apply_eligibility[0].output.system_node_group_id
    FS2_EDGE_GATE_RUN_ID                      = var.run_id
    FS2_EDGE_GATE_EXPECTED_NODE_COUNT         = tostring(terraform_data.public_edge_apply_eligibility[0].output.expected_node_count)
    FS2_EDGE_GATE_MINIMUM_DOMAINS             = tostring(terraform_data.public_edge_apply_eligibility[0].output.minimum_hostname_domains)
    FS2_EDGE_GATE_MAXIMUM_SURGE_MEMBERS       = tostring(terraform_data.public_edge_apply_eligibility[0].output.maximum_surge_members)
    FS2_EDGE_GATE_NODE_SELECTOR_JSON          = jsonencode(var.public_edge_availability_contract.node_selector)
    FS2_EDGE_GATE_POLICY_SHA256                = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_policy_sha256
    FS2_EDGE_GATE_BINDING_SHA256               = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_binding_sha256
    FS2_EDGE_GATE_CAS_POLICY_SHA256            = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_cas_policy_sha256
    FS2_EDGE_GATE_CAS_BINDING_SHA256           = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_cas_binding_sha256
    FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256      = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_bootstrap_policy_sha256
    FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256     = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_bootstrap_binding_sha256
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.api_version
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND        = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.kind
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME        = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.name
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE    = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.resource
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256      = data.terraform_remote_state.foundation.outputs.cluster_contract.public_edge_membership_authority.admission_boundary_approval.sha256
    FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256       = local.public_edge_membership_trust_sha256
    FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256 = local.public_edge_provider_adapter_trust_sha256
  }

  depends_on = [terraform_data.public_edge_apply_eligibility]
}
