variable "pools" {
  description = "Accelerator pool quota facts. Resource and flavor names come from the provider-neutral accelerator-pool contract."
  type = map(object({
    flavor_name   = string
    resource_name = string
    capacity      = number
  }))

  validation {
    condition = length(var.pools) > 0 && length(var.pools) <= 32 && alltrue([
      for pool_id, pool in var.pools :
      length(pool_id) <= 63 &&
      can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", pool_id)) &&
      length(pool.flavor_name) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", pool.flavor_name)) &&
      length(pool.resource_name) <= 317 &&
      length(split("/", pool.resource_name)) == 2 &&
      length(split("/", pool.resource_name)[0]) <= 253 &&
      length(split("/", pool.resource_name)[1]) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*/[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$", pool.resource_name)) &&
      floor(pool.capacity) == pool.capacity && pool.capacity >= 0
    ])
    error_message = "pools must contain 1-32 label-safe IDs and ResourceFlavor names of at most 63 characters, Kubernetes extended-resource names whose prefix is at most 253 characters and name at most 63, so at most 317 in total, and nonnegative whole accelerator capacities."
  }
}

variable "default_queue" {
  description = "Stable queue identities retained for upgrades and existing admitted serving workloads."
  type = object({
    cluster_queue_name = string
    local_queue_name   = string
    namespace          = string
    queueing_strategy  = string
    # Accepts Kueue's LocalQueue fair-share admission ordering on the stable
    # ClusterQueue once it serves more than one lane.
    fair_share_precedence_acknowledged = optional(bool, false)
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
      namespace = string
      # Every namespace this ClusterQueue admits Workloads from. The primary
      # namespace above is always included.
      namespaces             = optional(list(string), [])
      queueing_strategy      = string
      fair_sharing_weight    = number
      admission_fair_sharing = bool
      flavor_order           = list(string)
      flavor_fungibility = optional(object({
        when_can_borrow  = optional(string, "MayStopSearch")
        when_can_preempt = optional(string, "TryNextFlavor")
        preference       = optional(string)
      }), {})
      admission_checks = optional(list(object({
        name       = string
        on_flavors = optional(list(string), [])
      })), [])
      pool_quotas = map(object({
        nominal_quota   = number
        borrowing_limit = optional(number)
        lending_limit   = optional(number)
      }))
      # Kueue orders LocalQueues by fair-share usage before priority when
      # admission fair sharing is on; acknowledging that is how an operator
      # accepts multi-lane fair-share ordering instead of priority ordering.
      fair_share_precedence_acknowledged = optional(bool, false)
      # Core floor this queue holds when core admission is on. Whatever is left
      # of the exact totals stays in the Cohort, so a zero-floor queue can
      # still borrow core capacity for borrowed or scale-from-zero work.
      core_quota = optional(object({
        cpu_millicores = optional(number, 0)
        memory_mib     = optional(number, 0)
      }), {})
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
      tenant_ids          = optional(set(string), [])
      service_classes     = optional(set(string), [])
    }))
    service_classes = map(object({
      workload_priority_class = string
      priority                = number
      default_local_queue     = optional(string)
      preemption_mode         = string
      pool_preference         = list(string)
      max_queue_seconds       = optional(number)
      max_execution_seconds   = optional(number)
      description             = optional(string)
    }))
  })
}

variable "cpu_classes" {
  description = <<-EOT
    Named placement classes for CPU-only scientific stages.

    A CPU stage receives no accelerator ResourceFlavor, so it inherits no node
    labels and no tolerations, and on a tainted pool it would never schedule.
    Stages differ: an AlphaFold 3 data pipeline belongs on the tainted
    reference-data pool beside the shared databases, while a collector or
    aggregation stage belongs on general CPU capacity. Each class therefore
    carries its own queue, selector, toleration, and advertised schedulable
    capacity, and a consumer freezes the class per stage rather than applying
    one global placement to every CPU stage.
  EOT
  type = map(object({
    local_queue   = string
    cluster_queue = string
    namespace     = string
    pool_id       = optional(string)
    node_selector = optional(map(string), {})
    tolerations = optional(list(object({
      key      = string
      operator = string
      value    = optional(string)
      effect   = string
    })), [])
    schedulable_capacity = optional(object({
      cpu_millicores        = number
      memory_mib            = number
      ephemeral_storage_mib = number
    }))
  }))
  default = {}
}

variable "external_cluster_queues" {
  description = <<-EOT
    ClusterQueues another Terraform owner renders, with the namespaces they
    admit. A LocalQueue here may reference one of them; its quota, preemption,
    and flavor policy stay with its own owner. The reference-data CPU
    ClusterQueue is the current case.
  EOT
  type = map(object({
    namespaces = list(string)
    # The core quota that queue's own owner renders, so a CPU stage request can
    # be checked against the quota that will actually admit it.
    core_quota = optional(object({
      cpu_millicores = number
      memory_mib     = number
    }))
  }))
  default = {}
}

variable "core_capacity" {
  description = <<-EOT
    Exact aggregate schedulable cpu and memory across the pools that back this
    Kueue installation, plus the shared core ResourceFlavor name.

    Supplying this turns core-resource admission on: cpu and memory leave the
    Kueue exclusions and every ClusterQueue gets a core resourceGroup. The
    totals are operator-declared measurements of schedulable capacity, not a
    nominal vCPU count derived from a machine preset, because Kueue quota is
    only meaningful against what nodes actually offer.

    Leaving it null keeps cpu and memory excluded, and then no cpu/memory
    nominalQuota anywhere in the cluster is enforced.
  EOT
  type = object({
    flavor_name    = optional(string, "fs2-core")
    cpu_millicores = number
    memory_mib     = number
  })
  default  = null
  nullable = true
}

variable "cpu_stage_requests" {
  description = <<-EOT
    The largest per-Pod cpu and memory request each CPU stage class must be
    able to run, checked against that class's per-node schedulable capacity.
    One Pod has to fit one node, so a class whose nodes are smaller than its
    declared stage request is rejected instead of producing a Job that can
    never be scheduled.
  EOT
  type = map(object({
    cpu_millicores = number
    memory_mib     = number
  }))
  default = {}
}

variable "external_local_queues" {
  description = <<-EOT
    LocalQueues another Terraform owner already creates, described here only so
    routing and resolution can see them.

    The licensed academic queue is created by modules/academic-assets beside
    the claim and namespace it belongs to. Re-creating it here would give one
    API object two Terraform owners and would need a state move on upgrade, so
    this module validates and publishes it without managing it.
  EOT
  type = map(object({
    namespace           = string
    cluster_queue       = string
    fair_sharing_weight = optional(number, 1)
    model_ids           = optional(set(string), [])
    tenant_ids          = optional(set(string), [])
    service_classes     = optional(set(string), [])
  }))
  default = {}
}

variable "required_namespaces" {
  description = <<-EOT
    Namespaces a named ClusterQueue must admit, whatever the operator wrote.
    A caller derives these from a feature whose assets exist in exactly one
    namespace, so they survive an explicit override of the stable ClusterQueue
    entry instead of being lost to it.
  EOT
  type        = map(list(string))
  default     = {}
}

variable "namespace_bound_models" {
  description = "Models whose runtime assets exist in exactly one namespace, such as a licensed academic claim. A consumer must refuse rather than fall back to another namespace."
  type        = map(string)
  default     = {}
}

variable "base_priority_classes" {
  description = "Existing model-serving WorkloadPriorityClasses that must remain available during migration."
  type        = map(number)
  default     = {}

  validation {
    condition = alltrue([
      for name, value in var.base_priority_classes :
      length(name) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", name)) &&
      floor(value) == value && value >= -2147483648 && value <= 2147483647
    ])
    error_message = "Base WorkloadPriorityClass names must be DNS label values of at most 63 characters and priorities must be signed int32 integers."
  }
}

variable "labels" {
  description = "Common labels applied to rendered Kueue policy objects."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key, value in var.labels :
      length(key) <= 317 &&
      length(split("/", key)) <= 2 &&
      (length(split("/", key)) == 1 || length(split("/", key)[0]) <= 253) &&
      can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key)) &&
      length(value) <= 63 &&
      (value == "" || can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", value)))
    ])
    error_message = "Kueue common labels must use Kubernetes label-key grammar and label values of at most 63 characters."
  }
}

variable "annotations" {
  description = "Common non-secret provenance annotations applied to rendered Kueue policy objects."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key, value in var.annotations :
      length(key) <= 317 &&
      length(split("/", key)) <= 2 &&
      (length(split("/", key)) == 1 || length(split("/", key)[0]) <= 253) &&
      can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key)) &&
      # Kubernetes bounds annotations in bytes. Base64 gives the exact byte
      # length where Terraform's length() would count characters.
      (length(base64encode(value)) / 4 * 3) - length(regexall("=", base64encode(value))) <= 65536
    ])
    error_message = "Kueue common annotation keys must use Kubernetes qualified-name grammar and values must remain within 65536 UTF-8 bytes."
  }
}
