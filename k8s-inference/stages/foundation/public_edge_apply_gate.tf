# A Terraform Kubernetes data source is refreshed while a plan is created, not
# while that saved plan is later applied.  Re-read both authorities here, during
# apply, immediately before any public-edge Redis/Sentinel Pod can be created.
# `plantimestamp()` also makes every new saved plan replace this creation-only
# receipt. Its four-hour bound accommodates the declared prerequisite Jobs and
# waits; the mutation fence timestamps fresh provider/Node reads after those
# prerequisites instead of incorrectly spending a five-minute window on them.
locals {
  public_edge_gate_launcher_path = "/usr/local/libexec/fs2-public-edge-current/launcher"
  public_edge_gate_verifier_path = "${path.module}/scripts/verify-public-edge-node-eligibility.py"
  public_edge_gate_verifier_sha256 = filesha256(local.public_edge_gate_verifier_path)
  public_edge_membership_trust_sha256 = filesha256("${path.module}/trusted-public-edge-membership-issuers.json")
  public_edge_provider_adapter_trust_sha256 = filesha256("${path.module}/trusted-public-edge-provider-adapters.json")
  public_edge_preventive_boundary_trust_sha256 = filesha256("${path.module}/trusted-public-edge-preventive-boundary-issuers.json")
}

resource "terraform_data" "public_edge_apply_eligibility" {
  count = local.public_edge_enabled ? 1 : 0

  input = {
    schema                    = "fs2-serve.nebius.ai/public-edge-apply-eligibility/v1"
    stage                     = "foundation"
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

  # Creation-only and read-only: destroy removes only the Terraform receipt.
  # The verifier lists/gets the exact provider NodeGroup and Kubernetes Nodes;
  # it never creates, patches, labels, cordons, drains, or deletes an object.
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
      FS2_EDGE_GATE_POLICY_SHA256          = local.public_edge_node_authority_policy_sha256
      FS2_EDGE_GATE_BINDING_SHA256         = local.public_edge_node_authority_binding_sha256
      FS2_EDGE_GATE_CAS_POLICY_SHA256      = local.public_edge_node_authority_cas_policy_sha256
      FS2_EDGE_GATE_CAS_BINDING_SHA256     = local.public_edge_node_authority_cas_binding_sha256
      FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256  = local.public_edge_cas_bootstrap_policy_sha256
      FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256 = local.public_edge_cas_bootstrap_binding_sha256
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION = local.public_edge_cas_bootstrap_authority.approval_api_version
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND        = local.public_edge_cas_bootstrap_authority.approval_kind
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME        = local.public_edge_cas_bootstrap_authority.approval_name
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE    = local.public_edge_cas_bootstrap_authority.approval_resource
      FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256      = local.public_edge_node_authority_approval_sha256
      FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256 = local.public_edge_membership_trust_sha256
      FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256 = local.public_edge_provider_adapter_trust_sha256
      FS2_EDGE_GATE_PREVENTIVE_BOUNDARY_TRUST_SHA256 = local.public_edge_preventive_boundary_trust_sha256
    }
  }

  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_namespace_v1.platform["envoy-gateway-system"],
    kubernetes_manifest.public_edge_node_authority_binding,
    kubernetes_config_map_v1.edge_rate_limit_redis,
    kubernetes_service_v1.edge_rate_limit_redis_headless,
    kubernetes_service_v1.edge_rate_limit_redis_sentinel,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_service,
    kubernetes_network_policy_v1.edge_rate_limit_redis,
  ]
}

# Re-run the complete authority join as a deferred data read consumed by the
# protected resource's own lifecycle precondition. Unlike a standalone
# provisioner receipt, this result is evaluated in the StatefulSet mutation
# path after the creation gate and cannot be omitted while leaving that
# mutation enabled. Kubernetes scheduling remains the final live fence at each
# Pod bind: the exact selector, hard-taint eligibility and required anti-
# affinity are all re-evaluated by the scheduler.
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
    FS2_EDGE_GATE_STAGE                       = "foundation-mutation"
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
    FS2_EDGE_GATE_POLICY_SHA256                = local.public_edge_node_authority_policy_sha256
    FS2_EDGE_GATE_BINDING_SHA256               = local.public_edge_node_authority_binding_sha256
    FS2_EDGE_GATE_CAS_POLICY_SHA256            = local.public_edge_node_authority_cas_policy_sha256
    FS2_EDGE_GATE_CAS_BINDING_SHA256           = local.public_edge_node_authority_cas_binding_sha256
    FS2_EDGE_GATE_BOOTSTRAP_POLICY_SHA256      = local.public_edge_cas_bootstrap_policy_sha256
    FS2_EDGE_GATE_BOOTSTRAP_BINDING_SHA256     = local.public_edge_cas_bootstrap_binding_sha256
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_API_VERSION = local.public_edge_cas_bootstrap_authority.approval_api_version
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_KIND        = local.public_edge_cas_bootstrap_authority.approval_kind
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_NAME        = local.public_edge_cas_bootstrap_authority.approval_name
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_RESOURCE    = local.public_edge_cas_bootstrap_authority.approval_resource
    FS2_EDGE_GATE_BOUNDARY_APPROVAL_SHA256      = local.public_edge_node_authority_approval_sha256
    FS2_EDGE_GATE_MEMBERSHIP_TRUST_SHA256       = local.public_edge_membership_trust_sha256
    FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256 = local.public_edge_provider_adapter_trust_sha256
    FS2_EDGE_GATE_PREVENTIVE_BOUNDARY_TRUST_SHA256 = local.public_edge_preventive_boundary_trust_sha256
  }

  depends_on = [
    terraform_data.public_edge_apply_eligibility,
    kubernetes_config_map_v1.edge_rate_limit_redis,
    kubernetes_service_v1.edge_rate_limit_redis_headless,
    kubernetes_service_v1.edge_rate_limit_redis_sentinel,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_service,
    kubernetes_network_policy_v1.edge_rate_limit_redis,
  ]
}
