# Explicit operator opt-in after managed-image ownership and native verbs
# qualification. Keep the allocator and its scheduling facts in the same
# generated stage inputs, so a later foundation apply cannot erase the plugin.
variable "managed_rdma_pools" {
  description = "Provider-managed eight-GPU pools with a verified allocator-only RDMA deployment, keyed by exact pool and GPU-cluster identity."
  type        = map(object({ gpu_cluster_id = string }))
  default     = {}
  validation {
    condition = alltrue([
      for pool in values(var.managed_rdma_pools) :
      can(regex("^computegpucluster-[a-z0-9]+$", pool.gpu_cluster_id))
    ])
    error_message = "RDMA opt-in requires exact provider GPU-cluster identities."
  }
}
