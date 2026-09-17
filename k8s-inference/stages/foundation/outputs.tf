output "cluster_contract" {
  description = "Non-secret identity passed verbatim to the workloads state."
  value = {
    cluster_id                        = var.cluster_id
    cluster_name                      = var.cluster_name
    kube_context                      = var.kube_context
    kube_system_uid                   = var.kube_system_uid
    project_sha256                    = nonsensitive(sha256(var.project_id))
    target_contract                   = var.target_contract
    target_sha256                     = local.target_contract_sha256
    target_region                     = local.selected_target.region
    run_id                            = var.run_id
    accelerator_pool_contract         = var.accelerator_pool_contract
    accelerator_pool_contract_sha256  = local.accelerator_pool_contract_sha256
    infrastructure_contract           = var.infrastructure_contract
    infrastructure_contract_sha256    = local.infrastructure_contract_sha256
    public_edge_availability_contract         = var.public_edge_availability_contract
    public_edge_availability_contract_sha256  = local.public_edge_availability_contract_sha256
    public_edge_ready_node_preflight          = local.public_edge_ready_node_preflight
    public_edge_membership_authority = merge(local.public_edge_membership_authority, {
      admission_policy_sha256  = local.public_edge_node_authority_policy_sha256
      admission_binding_sha256 = local.public_edge_node_authority_binding_sha256
      admission_cas_policy_sha256  = local.public_edge_node_authority_cas_policy_sha256
      admission_cas_binding_sha256 = local.public_edge_node_authority_cas_binding_sha256
      admission_bootstrap_policy_sha256  = local.public_edge_cas_bootstrap_policy_sha256
      admission_bootstrap_binding_sha256 = local.public_edge_cas_bootstrap_binding_sha256
      admission_boundary_approval = {
        api_version = try(local.public_edge_cas_bootstrap_authority.approval_api_version, "")
        kind        = try(local.public_edge_cas_bootstrap_authority.approval_kind, "")
        name        = try(local.public_edge_cas_bootstrap_authority.approval_name, "")
        resource    = try(local.public_edge_cas_bootstrap_authority.approval_resource, "")
        sha256      = local.public_edge_node_authority_approval_sha256
        preventive_boundary_receipt_sha256 = try(local.public_edge_observed_preventive_boundary.receipt_sha256, "")
      }
    })
    jobset                                    = var.jobset.enabled ? module.jobset_controller[0].contract : null
  }
}

output "managed_resource_count" {
  description = "Expected managed Terraform address count for plan review."
  # 29 pre-existing addresses, the Kueue release verification, and the always
  # present jobset-system namespace. The seven exact rate-limit-store/service
  # addresses below are always present. Public mode adds the apply-time eligibility gate
  # and four Node-authority/CAS admission addresses to the same closed allowlist.
  # The JobSet module itself contributes five
  # addresses only when it is enabled.
  value = (
    31 + length(local.edge_rate_limit_managed_resource_addresses) +
    (nonsensitive(var.bootstrap_grafana_credentials == null) ? 0 : 1) +
    (var.jobset.enabled ? 5 : 0)
  )
}

output "edge_rate_limit_managed_resource_addresses" {
  description = "Closed allowlist of HA rate-limit-store and public-edge apply-gate Terraform addresses included in managed_resource_count."
  value       = local.edge_rate_limit_managed_resource_addresses
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
