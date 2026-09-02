variable "pools" {
  description = "Accelerator pool quota facts. Resource and flavor names come from the provider-neutral accelerator-pool contract."
  type = map(object({
    flavor_name   = string
    resource_name = string
    capacity      = number
  }))

  validation {
    condition = length(var.pools) > 0 && alltrue([
      for pool_id, pool in var.pools :
      can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", pool_id)) &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", pool.flavor_name)) &&
      can(regex("^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", pool.resource_name)) &&
      floor(pool.capacity) == pool.capacity && pool.capacity >= 0
    ])
    error_message = "pools must contain DNS-safe IDs/flavors, Kubernetes extended-resource names, and nonnegative whole accelerator capacities."
  }
}

variable "default_queue" {
  description = "Stable queue identities retained for upgrades and existing admitted serving workloads."
  type = object({
    cluster_queue_name = string
    local_queue_name   = string
    namespace          = string
    queueing_strategy  = string
  })
}

variable "scheduling" {
  description = "Operator scheduling policy. Empty queue maps retain the stable single-queue topology."
  type = object({
    cohort = object({
      enabled             = bool
      name                = string
      fair_sharing_weight = number
    })
    cluster_queues = map(object({
      namespace              = string
      queueing_strategy      = string
      fair_sharing_weight    = number
      admission_fair_sharing = bool
      flavor_order           = list(string)
      pool_quotas = map(object({
        nominal_quota   = number
        borrowing_limit = optional(number)
        lending_limit   = optional(number)
      }))
      preemption = object({
        reclaim_within_cohort = string
        within_cluster_queue  = string
      })
    }))
    local_queues = map(object({
      namespace           = string
      cluster_queue       = string
      fair_sharing_weight = number
      model_ids           = set(string)
    }))
    service_classes = map(object({
      workload_priority_class = string
      priority                = number
      default_local_queue     = optional(string)
      preemption_mode         = string
      pool_preference         = list(string)
    }))
  })
}

variable "base_priority_classes" {
  description = "Existing model-serving WorkloadPriorityClasses that must remain available during migration."
  type        = map(number)
  default     = {}
}

variable "labels" {
  description = "Common labels applied to rendered Kueue policy objects."
  type        = map(string)
  default     = {}
}

variable "annotations" {
  description = "Common non-secret provenance annotations applied to rendered Kueue policy objects."
  type        = map(string)
  default     = {}
}
