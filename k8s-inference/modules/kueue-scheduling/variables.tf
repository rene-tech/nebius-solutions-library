variable "pools" {
  description = "Accelerator pool quota facts. Resource and flavor names come from the provider-neutral accelerator-pool contract."
  type = map(object({
    flavor_name   = string
    resource_name = string
    capacity      = number
    # Whether this pool's capacity can be reclaimed by the provider. Kueue
    # tries flavors in the queue's order, so a reclaimable pool searched ahead
    # of an always-available one sends work to capacity that can be taken
    # away while stable capacity sits idle. The renderer needs the fact to
    # order flavors truthfully rather than alphabetically.
    preemptible = optional(bool, false)
    # Nodes the pool always keeps. A pool with a floor is hot: work placed
    # there starts without waiting for a scale-up.
    min_nodes = optional(number, 0)
  }))

  validation {
    condition = length(var.pools) > 0 && length(var.pools) <= 32 && alltrue([
      for pool_id, pool in var.pools :
      length(pool_id) <= 63 &&
      # One canonical pool-ID grammar, shared with the CPU stage class schema:
      # a lowercase Kubernetes label value, not a DNS label, because real pool
      # IDs carry dots and underscores.
      can(regex("^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$", pool_id)) &&
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
    # Order Kueue tries ResourceFlavors in on the stable ClusterQueue. Empty
    # derives a deterministic warm-first order from preemptibility and the node
    # floor. An explicit order may reorder equally stable pools, but it cannot
    # put a colder tier ahead of a warmer one.
    flavor_order = optional(list(string), [])
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
    Named placement classes for CPU-only scientific stages, assembled here.

    Several Terraform owners contribute entries; this module is the sole
    assembler and emits the merged map as `cpu_classes` in the single
    content-addressed scheduling ConfigMap, against
    catalog/runtime/schema/cpu-stage-classes.schema.json.

    A CPU stage receives no accelerator ResourceFlavor, so it inherits no node
    labels and no tolerations, and on a tainted pool it would never schedule.
    Stages differ: an AlphaFold 3 data pipeline belongs on the tainted
    reference-data pool beside the shared databases, while a collector or
    aggregation stage belongs on general CPU capacity. Each class therefore
    carries its own queue identity, node routing, tolerations, per-node
    capacity, and how the pool it actually ran on is determined, and a
    consumer freezes the class per stage rather than applying one global
    placement to every CPU stage.
  EOT
  type = map(object({
    # A class is one LocalQueue in one namespace. A consumer that keys
    # LocalQueues by bare name cannot represent the same name in two
    # namespaces, so a producer that runs a lane in several namespaces
    # contributes one class per namespace.
    local_queue   = string
    cluster_queue = string
    namespace     = string
    # The ResourceFlavor a CPU admission reports for cpu and memory. The
    # accelerator reverse map does not cover it, so a collector maps an actual
    # cpu/memory flavor back to this class through this value and refuses a
    # mismatch instead of guessing. It identifies the class, not the pool.
    resource_flavor = string
    # Every pool a stage of this class may land on: the expected set, never
    # the outcome.
    eligible_pool_ids = list(string)
    # How the pool it actually landed on is determined. One flavor covering
    # several pools cannot answer that from the admission, so such a class
    # must name the Node label a consumer reads after scheduling.
    pool_resolution = object({
      mode           = string
      pool_id        = optional(string)
      node_label_key = optional(string)
    })
    # Required, not optional. A class without node routing, an explicit
    # toleration list, and per-node capacity is not an executable placement:
    # the Pod would either miss the pool entirely or be admitted to a node it
    # cannot fit. An empty toleration list is the correct value for untainted
    # capacity, and stating it is different from omitting it.
    node_selector = map(string)
    tolerations = list(object({
      key      = string
      operator = string
      # Set for operator Equal, absent for operator Exists.
      value  = optional(string)
      effect = string
    }))
    # Both the raw observed quantities and the normalized integers, so a
    # consumer can compare against a Pod request without a lossy round trip.
    schedulable_capacity = object({
      cpu               = string
      memory            = string
      ephemeral_storage = string
      cpu_millicores    = number
      memory_mib        = number
      # Zero means the pool advertises no ephemeral storage budget, which is
      # different from a pool that advertises some.
      ephemeral_storage_mib = number
    })
  }))
  default = {}

  validation {
    condition = alltrue([
      for class_name, class in var.cpu_classes :
      length(class.node_selector) >= 1 &&
      length(class.node_selector) <= 16 &&
      # The same qualified-name and label-value grammars the published class
      # schema states, so a class that validates here validates there.
      alltrue([
        for label_key, label_value in class.node_selector :
        # A qualified name bounds each half separately: at most 253 before the
        # slash and 63 after. A total-length check alone would accept a
        # 254-character prefix beside a short name, which the API rejects.
        length(label_key) <= 317 &&
        length(split("/", label_key)) <= 2 &&
        length(split("/", label_key)[0]) <= (length(split("/", label_key)) == 2 ? 253 : 63) &&
        (length(split("/", label_key)) == 1 || length(element(split("/", label_key), 1)) <= 63) &&
        can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", label_key)) &&
        length(label_value) <= 63 &&
        can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", label_value))
      ]) &&
      length(class.tolerations) <= 8 &&
      alltrue([
        for toleration in class.tolerations :
        contains(["Equal", "Exists"], toleration.operator) &&
        contains(["NoSchedule", "PreferNoSchedule", "NoExecute"], toleration.effect) &&
        length(toleration.key) <= 317 &&
        length(split("/", toleration.key)) <= 2 &&
        length(split("/", toleration.key)[0]) <= (length(split("/", toleration.key)) == 2 ? 253 : 63) &&
        (length(split("/", toleration.key)) == 1 || length(element(split("/", toleration.key), 1)) <= 63) &&
        can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", toleration.key)) &&
        (
          toleration.operator == "Equal"
          # A value the API would reject is not a toleration, whatever its
          # length: a Kubernetes label value has its own grammar.
          ? try(length(toleration.value), 0) >= 1 &&
          length(toleration.value) <= 63 &&
          can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", toleration.value))
          : try(toleration.value, null) == null
        )
      ]) &&
      floor(class.schedulable_capacity.cpu_millicores) == class.schedulable_capacity.cpu_millicores &&
      class.schedulable_capacity.cpu_millicores >= 1 &&
      floor(class.schedulable_capacity.memory_mib) == class.schedulable_capacity.memory_mib &&
      class.schedulable_capacity.memory_mib >= 1 &&
      floor(class.schedulable_capacity.ephemeral_storage_mib) == class.schedulable_capacity.ephemeral_storage_mib &&
      class.schedulable_capacity.ephemeral_storage_mib >= 0 &&
      # One canonical spelling, so the quantity and the integer beside it are
      # the same number and a consumer may read either. Two independent
      # fields with only a grammar check would let cpu = "1m" sit beside
      # cpu_millicores = 30000 and the contract would be lying about one of
      # them.
      class.schedulable_capacity.cpu == "${class.schedulable_capacity.cpu_millicores}m" &&
      class.schedulable_capacity.memory == "${class.schedulable_capacity.memory_mib}Mi" &&
      class.schedulable_capacity.ephemeral_storage ==
      "${class.schedulable_capacity.ephemeral_storage_mib}Mi"
    ])
    error_message = "A CPU stage class must be an executable placement: 1-16 node-selector labels, at most 8 tolerations whose operator is Equal with a value or Exists without one, and measured per-node capacity with positive whole cpu and memory and nonnegative ephemeral storage. Each capacity quantity must be the exact canonical spelling of the integer beside it: cpu is \"<cpu_millicores>m\" and memory and ephemeral storage are \"<mib>Mi\", so the two spellings can never disagree."
  }

  validation {
    condition = alltrue([
      for class_name, class in var.cpu_classes :
      length(class.eligible_pool_ids) >= 1 &&
      length(class.eligible_pool_ids) <= 32 &&
      length(class.eligible_pool_ids) == length(distinct(class.eligible_pool_ids)) &&
      # Pool IDs are lowercase label values, not DNS labels: real IDs carry
      # dots and underscores. One grammar, shared with the class schema.
      alltrue([
        for pool_id in class.eligible_pool_ids :
        length(pool_id) <= 63 &&
        can(regex("^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$", pool_id))
      ]) &&
      contains(["per-pool-flavor", "node-label-observation"], class.pool_resolution.mode) &&
      (
        class.pool_resolution.mode == "per-pool-flavor"
        ? (
          length(class.eligible_pool_ids) == 1 &&
          try(class.pool_resolution.pool_id, null) == class.eligible_pool_ids[0] &&
          try(class.pool_resolution.node_label_key, null) == null
        )
        : (
          try(class.pool_resolution.pool_id, null) == null &&
          try(length(class.pool_resolution.node_label_key), 0) >= 1 &&
          length(class.pool_resolution.node_label_key) <= 317 &&
          length(split("/", class.pool_resolution.node_label_key)) <= 2 &&
          length(split("/", class.pool_resolution.node_label_key)[0]) <= (
            length(split("/", class.pool_resolution.node_label_key)) == 2 ? 253 : 63
          ) &&
          (
            length(split("/", class.pool_resolution.node_label_key)) == 1 ||
            length(element(split("/", class.pool_resolution.node_label_key), 1)) <= 63
          ) &&
          can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", class.pool_resolution.node_label_key))
        )
      )
    ])
    error_message = "A CPU stage class must list 1-32 distinct eligible pools and say how the actual pool is determined: per-pool-flavor names its single pool and nothing else, and node-label-observation names the Node label a consumer reads after scheduling and claims no pool. A ResourceFlavor covering several pools cannot report which one ran the stage."
  }

  validation {
    # A consumer keys LocalQueues by bare name, so the same name in two
    # namespaces is unrepresentable rather than merely confusing.
    condition = length(distinct([
      for class in values(var.cpu_classes) : class.local_queue
      ])) == length(var.cpu_classes) && length(distinct([
      for class in values(var.cpu_classes) : class.resource_flavor
    ])) == length(var.cpu_classes)
    error_message = "Each CPU stage class must own its LocalQueue name and its cpu/memory ResourceFlavor outright; a name shared by two classes cannot be resolved back to one placement."
  }
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
    Measured schedulable cpu and memory per accelerator pool, at that pool's
    maximum node count, keyed by pool ID.

    Supplying it turns core-resource admission on: cpu and memory leave the
    Kueue exclusions and join the accelerator resourceGroup of every
    ClusterQueue. That coupling is the point. Kueue assigns exactly one
    ResourceFlavor per resourceGroup per PodSet, so putting cpu, memory and
    the accelerator resource in one group makes a Workload's cpu and memory
    come from the same pool as its accelerators. A separate core flavor would
    let a Workload reserve accelerators in one pool and core capacity measured
    on another, and then never fit any node.

    The numbers are measured Kubernetes allocatable, not a machine preset's
    nominal size, because quota is only meaningful against what nodes offer.

    Empty keeps cpu and memory excluded, and then no cpu/memory nominalQuota
    anywhere in the cluster is enforced.
  EOT
  type = map(object({
    cpu_millicores = number
    memory_mib     = number
  }))
  default  = {}
  nullable = false

  validation {
    condition = alltrue([
      for pool_id, capacity in var.core_capacity :
      floor(capacity.cpu_millicores) == capacity.cpu_millicores &&
      capacity.cpu_millicores >= 1 &&
      floor(capacity.memory_mib) == capacity.memory_mib &&
      capacity.memory_mib >= 1
    ])
    error_message = "Each pool's core capacity must be whole and positive cpu millicores and memory MiB."
  }
}

variable "model_eligible_pool_ids" {
  description = <<-EOT
    Pools each selected model is qualified to run on, taken from the
    authoritative model placement contract.

    A service class lists every deployed pool in its search order, so the
    order alone says nothing about whether a given model can run on the head
    of it. A consumer intersects this map with the class's order and refuses
    when the intersection is empty, rather than inferring eligibility from a
    pool name.
  EOT
  type        = map(list(string))
  default     = {}
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
