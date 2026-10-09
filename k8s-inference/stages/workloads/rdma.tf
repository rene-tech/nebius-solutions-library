# Same explicit opt-in as foundation's allocator; never infer working verbs
# from an accelerator name or GPU-cluster topology alone.
variable "managed_rdma_pools" {
  type    = map(object({ gpu_cluster_id = string }))
  default = {}
  validation {
    condition = alltrue([
      for pool_id, rdma in var.managed_rdma_pools : try(
        var.accelerator_pool_contract.pools[pool_id].node.topology == "gpu_cluster" &&
        var.accelerator_pool_contract.pools[pool_id].node.gpus_per_node == 8 &&
        var.accelerator_pool_contract.pools[pool_id].provider.driver.owner == "provider-managed" &&
        can(regex("^computegpucluster-[a-z0-9]+$", rdma.gpu_cluster_id)),
        false,
      )
    ])
    error_message = "RDMA scheduling requires an existing provider-managed eight-GPU pool and exact GPU-cluster identity."
  }
}

locals {
  rdma_resource_name = "rdma.fs2.nebius/hca"
  rdma_node_capacity = {
    for pool_id, rdma in var.managed_rdma_pools : pool_id => {
      extended_resources = { (local.rdma_resource_name) = 1 }
      node_labels = {
        "topology.fs2.nebius/scope"          = "gpu_cluster"
        "topology.nebius.com/gpu-cluster-id" = rdma.gpu_cluster_id
      }
    }
  }
  rdma_pool_capacity = {
    for pool_id in keys(var.managed_rdma_pools) : pool_id => {
      (local.rdma_resource_name) = var.accelerator_pool_contract.pools[pool_id].capacity.max_nodes
    } if contains(keys(local.selected_queue_pools), pool_id)
  }
}
