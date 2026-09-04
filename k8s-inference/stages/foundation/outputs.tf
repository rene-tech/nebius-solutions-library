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
  # 29 pre-existing addresses, the Kueue release verification, and the always
  # present jobset-system namespace. The JobSet module itself contributes five
  # addresses only when it is enabled.
  value = (
    31 +
    (nonsensitive(var.bootstrap_grafana_credentials == null) ? 0 : 1) +
    (var.jobset.enabled ? 5 : 0) +
    (var.alertmanager.enabled ? 1 : 0)
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
