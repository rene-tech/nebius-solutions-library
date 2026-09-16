output "cluster_contract" {
  description = "Non-secret identity passed verbatim to the workloads state."
  value = {
    cluster_id                       = var.cluster_id
    cluster_name                     = var.cluster_name
    kube_context                     = var.kube_context
    kube_system_uid                  = var.kube_system_uid
    project_sha256                   = nonsensitive(sha256(var.project_id))
    target_contract                  = var.target_contract
    target_sha256                    = local.target_contract_sha256
    target_region                    = local.selected_target.region
    run_id                           = var.run_id
    accelerator_pool_contract        = var.accelerator_pool_contract
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    infrastructure_contract          = var.infrastructure_contract
    infrastructure_contract_sha256   = local.infrastructure_contract_sha256
    jobset                           = var.jobset.enabled ? module.jobset_controller[0].contract : null
  }
}

output "managed_resource_count" {
  description = "Expected managed Terraform address count for plan review."
  # 29 pre-existing addresses, Kueue verification, jobset-system namespace,
  # and 18 deletion-protected NetworkPolicy-boundary/security-owner addresses. The JobSet
  # module itself contributes five addresses only when it is enabled.
  value = (
    49 +
    (nonsensitive(var.bootstrap_grafana_credentials == null) ? 0 : 1) +
    (var.jobset.enabled ? 5 : 0)
  )
}

output "jobset_contract" {
  description = "Pinned JobSet chart/image/API compatibility and readiness identity, or null when disabled."
  value       = var.jobset.enabled ? module.jobset_controller[0].contract : null
}

output "jobset_managed_resource_addresses" {
  description = "Closed JobSet state-address allowlist including its conditional namespace."
  # The jobset-system namespace is created unconditionally with the other
  # platform namespaces, so it belongs to the base count rather than here.
  value = var.jobset.enabled ? [
    for address in module.jobset_controller[0].managed_resource_addresses :
    "module.jobset_controller[0].${address}"
  ] : []
}

output "component_versions" {
  value = local.chart_versions
}

output "grafana_admin_secret_ref" {
  description = "Non-secret keys needed by workload acceptance to verify Grafana provisioning."
  value       = var.grafana_admin_secret_ref
}

output "network_policy_boundary_contract" {
  description = "Permanent foundation-owned Envoy policy identities consumed by workloads without transferring lifecycle ownership."
  value = {
    schema                  = "fs2-serve.nebius.ai/network-policy-boundary/v1"
    owner_stage             = "foundation"
    deletion_protected      = true
    external_security_owner = true
    mode                    = var.network_policy_boundary.mode
    gateway_namespace       = local.control_plane_network_policy_gateway_namespace
    controller_namespace    = local.control_plane_network_policy_controller_namespace
    service_account         = local.control_plane_network_policy_service_account
    security_owner          = local.control_plane_network_policy_security_owner
    lease_name              = local.control_plane_network_policy_state_name
    receipt_name            = local.control_plane_network_policy_state_name
    topology_name           = local.control_plane_network_policy_topology_name
    policy_names            = local.control_plane_network_policy_names
    admission_policy        = "fs2-network-policy-boundary"
    admission_binding       = "fs2-network-policy-boundary"
  }
}
