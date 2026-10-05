# Explicit opt-in only after verifying that the provider image supplies the
# loaded RDMA/GPU driver stack but no RDMA allocator. Never add another OFED or
# GPU driver owner. Existing GPU-operator/NVLink-rack paths remain unchanged.
variable "managed_rdma_pools" {
  type    = map(object({ gpu_cluster_id = string }))
  default = {}
  validation {
    condition     = length(var.managed_rdma_pools) <= 8
    error_message = "RDMA allocator opt-in must be bounded to at most eight exact pools."
  }
  validation {
    condition = alltrue([
      for pool_id in keys(var.managed_rdma_pools) : try(
        var.accelerator_pool_contract.pools[pool_id].node.topology == "gpu_cluster" &&
        var.accelerator_pool_contract.pools[pool_id].node.gpus_per_node == 8 &&
        var.accelerator_pool_contract.pools[pool_id].provider.driver.owner == "provider-managed",
        false,
      )
    ])
    error_message = "RDMA allocator-only opt-in requires an existing eight-GPU cluster pool with provider-owned drivers."
  }
}

module "managed_rdma_device_plugin" {
  for_each       = var.managed_rdma_pools
  source         = "../../modules/rdma-shared-device-plugin"
  pool_id        = each.key
  gpu_cluster_id = each.value.gpu_cluster_id
}
