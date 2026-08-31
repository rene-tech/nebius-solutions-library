locals {
  operator_gpu_pools = {
    for pool_id, pool in local.selected_gpu_pools : pool_id => pool
    if pool.provider.driver.owner == "gpu-operator"
  }
  managed_gpu_pools = {
    for pool_id, pool in local.selected_gpu_pools : pool_id => pool
    if pool.provider.driver.owner == "provider-managed"
  }
  network_operator_required = length(local.gpu_cluster_pools) > 0
  operator_mig_strategies = distinct([
    for pool in values(local.operator_gpu_pools) : pool.features.mig.mode
  ])
}

resource "terraform_data" "gpu_software_contract" {
  input = {
    managed_pool_ids         = sort(keys(local.managed_gpu_pools))
    operator_pool_ids        = sort(keys(local.operator_gpu_pools))
    network_operator_enabled = local.network_operator_required
    mig_strategy             = coalesce(try(one(local.operator_mig_strategies), null), "none")
  }

  lifecycle {
    precondition {
      condition     = length(local.operator_mig_strategies) <= 1
      error_message = "All operator-managed GPU pools in one cluster must use the same NVIDIA MIG strategy."
    }

    precondition {
      condition     = length(local.managed_gpu_pools) == 0 || length(local.operator_gpu_pools) == 0
      error_message = "Provider-managed GPU images and GPU Operator cannot share one cluster because both install a cluster-wide NVIDIA device-plugin stack. Select one driver owner for all GPU pools."
    }
  }
}

module "device_plugin" {
  count  = length(local.managed_gpu_pools) > 0 ? 1 : 0
  source = "../../../modules/device-plugin"

  cluster_id            = nebius_mk8s_v1_cluster.validation.id
  parent_id             = var.project_id
  dcgm_exporter_enabled = false

  depends_on = [terraform_data.gpu_software_contract]
}

module "gpu_operator" {
  count  = length(local.operator_gpu_pools) > 0 ? 1 : 0
  source = "../../../modules/gpu-operator"

  cluster_id                  = nebius_mk8s_v1_cluster.validation.id
  parent_id                   = var.project_id
  enable_dcgm_exporter        = false
  enable_dcgm_service_monitor = false
  mig_strategy                = coalesce(try(one(local.operator_mig_strategies), null), "none")
  cdi_enabled                 = true

  depends_on = [terraform_data.gpu_software_contract]
}

module "network_operator" {
  count  = local.network_operator_required ? 1 : 0
  source = "../../../modules/network-operator"

  cluster_id = nebius_mk8s_v1_cluster.validation.id
  parent_id  = var.project_id

  depends_on = [terraform_data.gpu_software_contract]
}
