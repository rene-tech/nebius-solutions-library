variable "pool_contract" {
  description = "General CPU pool facts from the infrastructure stage. Placement and capacity both come from here so a rendered class never restates a node identity."
  type = object({
    schema        = string
    node_selector = map(string)
    taint = object({
      key    = string
      value  = string
      effect = string
    })
    pools = map(object({
      id              = string
      name            = string
      platform        = string
      preset          = string
      capacity_type   = string
      elastic         = bool
      min_nodes       = number
      max_nodes       = number
      scale_from_zero = bool
      schedulable_capacity = object({
        cpu_millicores        = number
        memory_mib            = number
        ephemeral_storage_mib = number
      })
      shared_filesystem = bool
      node_labels       = map(string)
    }))
  })
}

variable "lane" {
  description = "Operator lane policy: queue identities, admission strategy, fair-sharing weight and the single execution namespace whose LocalQueue admits into this lane."
  type = object({
    enabled             = bool
    cluster_queue       = string
    local_queue         = string
    resource_flavor     = string
    queueing_strategy   = string
    fair_sharing_weight = number
    namespace           = string
  })
}

variable "reference_data_lane" {
  description = "The reference-data lane's queue, flavor and namespace identities, used only to prove the general lane reuses none of them. The reference-data class itself is produced and assembled by its own owner. Null when that plane is disabled."
  type = object({
    resource_flavor = string
    cluster_queue   = string
    local_queue     = string
    namespace       = string
  })
  default = null
}

variable "labels" {
  description = "Common labels applied to rendered Kueue objects."
  type        = map(string)
  default     = {}
}

variable "annotations" {
  description = "Common non-secret provenance annotations applied to rendered Kueue objects."
  type        = map(string)
  default     = {}
}
