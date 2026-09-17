# A Terraform Kubernetes data source is refreshed while a plan is created, not
# while that saved plan is later applied.  Re-read both authorities here, during
# apply, immediately before any public-edge Redis/Sentinel Pod can be created.
# `plantimestamp()` also makes every new saved plan replace this creation-only
# receipt. Its four-hour bound accommodates the declared prerequisite Jobs and
# waits; the mutation fence timestamps fresh provider/Node reads after those
# prerequisites instead of incorrectly spending a five-minute window on them.
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
    node_selector_sha256      = sha256(jsonencode(var.public_edge_availability_contract.node_selector))
    verifier_sha256           = filesha256("${path.module}/scripts/verify-public-edge-node-eligibility.py")
  }

  triggers_replace = {
    planned_at               = plantimestamp()
    cluster_id               = var.cluster_id
    kube_system_uid          = var.kube_system_uid
    system_node_group_id     = var.public_edge_availability_contract.system_node_group_id
    availability_contract_sha256 = local.public_edge_availability_contract_sha256
    verifier_sha256          = filesha256("${path.module}/scripts/verify-public-edge-node-eligibility.py")
  }

  # Creation-only and read-only: destroy removes only the Terraform receipt.
  # The verifier lists/gets the exact provider NodeGroup and Kubernetes Nodes;
  # it never creates, patches, labels, cordons, drains, or deletes an object.
  provisioner "local-exec" {
    command = "/usr/bin/python3 -I -B \"${path.module}/scripts/verify-public-edge-node-eligibility.py\""
    quiet   = true

    environment = {
      FS2_EDGE_GATE_STAGE                  = self.input.stage
      FS2_EDGE_GATE_PLANNED_AT             = self.input.planned_at
      FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS   = tostring(self.input.maximum_plan_age_seconds)
      FS2_EDGE_GATE_NEBIUS_PROFILE         = var.nebius_profile
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
      FS2_EDGE_GATE_NODE_SELECTOR_JSON     = jsonencode(var.public_edge_availability_contract.node_selector)
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
    "/usr/bin/python3",
    "-I",
    "-B",
    "${path.module}/scripts/verify-public-edge-node-eligibility.py",
    "--external",
  ]

  query = {
    gate_id                                  = terraform_data.public_edge_apply_eligibility[0].id
    verifier_sha256                          = filesha256("${path.module}/scripts/verify-public-edge-node-eligibility.py")
    FS2_EDGE_GATE_STAGE                       = "foundation-mutation"
    FS2_EDGE_GATE_PLANNED_AT                  = terraform_data.public_edge_apply_eligibility[0].output.planned_at
    FS2_EDGE_GATE_MAX_PLAN_AGE_SECONDS        = tostring(terraform_data.public_edge_apply_eligibility[0].output.maximum_plan_age_seconds)
    FS2_EDGE_GATE_NEBIUS_PROFILE              = var.nebius_profile
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
    FS2_EDGE_GATE_NODE_SELECTOR_JSON          = jsonencode(var.public_edge_availability_contract.node_selector)
  }

  depends_on = [
    terraform_data.public_edge_apply_eligibility,
    kubernetes_config_map_v1.edge_rate_limit_redis,
    kubernetes_service_v1.edge_rate_limit_redis_headless,
    kubernetes_service_v1.edge_rate_limit_redis_sentinel,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis,
    kubernetes_network_policy_v1.edge_rate_limit_redis,
  ]
}
