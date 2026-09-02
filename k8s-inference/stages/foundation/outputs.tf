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
  }
}

output "managed_resource_count" {
  description = "Expected managed Terraform address count for plan review."
  value       = 26 + (nonsensitive(var.bootstrap_grafana_credentials == null) ? 0 : 1)
}

output "component_versions" {
  value = local.chart_versions
}

output "grafana_admin_secret_ref" {
  description = "Non-secret keys needed by workload acceptance to verify Grafana provisioning."
  value       = var.grafana_admin_secret_ref
}
