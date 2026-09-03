locals {
  service_class_defaults = {
    platform-critical = {
      caller_selectable = false
      description       = "Cluster-control and recovery work that must not be displaced by customer workloads."
    }
    presentation = {
      caller_selectable = true
      description       = "Time-bounded live presentation work that outranks interactive and batch traffic."
    }
    interactive = {
      caller_selectable = true
      description       = "Latency-sensitive interactive proof-of-concept work."
    }
    customer-batch = {
      caller_selectable = true
      description       = "Ordinary customer batch work with fair access to shared capacity."
    }
    bulk-backfill = {
      caller_selectable = true
      description       = "Opportunistic bulk or backfill work that yields to higher service classes."
    }
  }
  # The classes whose callers expect to outrank ordinary batch work. Kueue can
  # only guarantee that inside one LocalQueue; see priority_precedence below.
  high_priority_service_classes = ["platform-critical", "presentation"]
  # platform-critical is resolver-internal, so a public caller can never
  # select it. Namespace-bound lane coverage is required for the rest.
  caller_selectable_service_classes = toset([
    for service_class, policy in local.service_class_defaults : service_class if policy.caller_selectable
  ])

  default_pool_quotas = {
    for pool_id, pool in var.pools : pool_id => {
      # A legacy single-queue deployment keeps all nominal quota. When an
      # operator adds tenant queues without restating the default queue, the
      # stable queue becomes an elastic zero-floor cohort member and all quota
      # not assigned as an explicit floor remains in the shared Cohort.
      nominal_quota   = length(var.scheduling.cluster_queues) == 0 ? pool.capacity : 0
      borrowing_limit = null
      lending_limit   = null
    }
  }
  default_cluster_queue = {
    namespace              = var.default_queue.namespace
    namespaces             = []
    queueing_strategy      = var.default_queue.queueing_strategy
    fair_sharing_weight    = 1
    admission_fair_sharing = true
    flavor_order           = sort(keys(var.pools))
    flavor_fungibility = {
      when_can_borrow  = "MayStopSearch"
      when_can_preempt = "TryNextFlavor"
      preference       = null
    }
    admission_checks                   = []
    pool_quotas                        = local.default_pool_quotas
    fair_share_precedence_acknowledged = var.default_queue.fair_share_precedence_acknowledged
    # A zero core floor: the stable queue borrows core capacity from the Cohort
    # rather than reserving it, so scale-from-zero and borrowed work still fit.
    core_quota = { cpu_millicores = 0, memory_mib = 0 }
    preemption = {
      # The stable serving queue is the source of presentation/interactive
      # work. It must be able to reclaim capacity loaned to a lower-priority
      # batch queue and to displace lower-priority work within itself.
      reclaim_within_cohort = "LowerPriority"
      within_cluster_queue  = "LowerPriority"
    }
  }
  default_local_queue = {
    namespace           = var.default_queue.namespace
    cluster_queue       = var.default_queue.cluster_queue_name
    fair_sharing_weight = 1
    model_ids           = toset([])
    tenant_ids          = toset([])
    service_classes     = toset([])
  }

  # The stable default objects always remain in the topology. An explicit entry
  # with the same name changes their policy without changing their Terraform or
  # Kubernetes identity, so already-admitted serving workloads survive rollout.
  cluster_queues = merge(
    { (var.default_queue.cluster_queue_name) = local.default_cluster_queue },
    var.scheduling.cluster_queues,
  )
  # Queues this module renders, plus queues another owner renders that must
  # still take part in routing and validation.
  managed_local_queues = merge(
    { (var.default_queue.local_queue_name) = local.default_local_queue },
    var.scheduling.local_queues,
  )
  external_local_queues = {
    for queue_name, queue in var.external_local_queues : queue_name => {
      namespace           = queue.namespace
      cluster_queue       = queue.cluster_queue
      fair_sharing_weight = queue.fair_sharing_weight
      model_ids           = queue.model_ids
      tenant_ids          = queue.tenant_ids
      service_classes     = queue.service_classes
    }
  }
  local_queues = merge(local.managed_local_queues, local.external_local_queues)
  # A ClusterQueue admits Workloads only from the namespaces its
  # namespaceSelector matches. Multi-namespace lanes (the licensed academic
  # claim namespace, for example) must therefore be listed explicitly.
  # One authoritative map for validation and for the contract: the queues this
  # module renders plus the queues another owner renders. Manifests are still
  # rendered only for the managed ones.
  cluster_queue_namespaces = merge(
    {
      for queue_name, queue in local.cluster_queues : queue_name => sort(distinct(concat(
        [queue.namespace],
        length(queue.namespaces) == 0 ? [] : queue.namespaces,
        try(var.required_namespaces[queue_name], []),
      )))
    },
    {
      for queue_name, queue in var.external_cluster_queues : queue_name => sort(distinct(concat(
        queue.namespaces,
        try(var.required_namespaces[queue_name], []),
      )))
    },
  )
  referenceable_cluster_queues = sort(distinct(concat(
    keys(local.cluster_queues),
    keys(var.external_cluster_queues),
  )))

  pool_ids       = sort(keys(var.pools))
  resource_names = sort(distinct([for pool in values(var.pools) : pool.resource_name]))

  # Core admission uses ONE label-less ResourceFlavor in its own resourceGroup.
  # Kueue allows a resource in exactly one group and a flavor in exactly one
  # group, so this is the only shape that budgets cpu and memory without
  # colliding with the accelerator groups, and it keeps working when the
  # deployment advertises several different accelerator resources.
  core_admission_enabled = var.core_capacity != null
  core_flavor_name       = local.core_admission_enabled ? var.core_capacity.flavor_name : null
  core_resource_flavor = !local.core_admission_enabled ? null : {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name        = local.core_flavor_name
      labels      = var.labels
      annotations = var.annotations
    }
    # No nodeLabels and no tolerations: core capacity is not pool-specific, and
    # the accelerator flavor already pins an accelerator Pod to its pool.
    spec = {}
  }
  core_floor_totals = {
    cpu_millicores = local.core_admission_enabled ? sum(concat([0], [
      for queue in values(local.cluster_queues) : queue.core_quota.cpu_millicores
    ])) : 0
    memory_mib = local.core_admission_enabled ? sum(concat([0], [
      for queue in values(local.cluster_queues) : queue.core_quota.memory_mib
    ])) : 0
  }
  core_shared_quota = {
    cpu_millicores = local.core_admission_enabled ? (
      var.core_capacity.cpu_millicores - local.core_floor_totals.cpu_millicores
    ) : 0
    memory_mib = local.core_admission_enabled ? (
      var.core_capacity.memory_mib - local.core_floor_totals.memory_mib
    ) : 0
  }
  core_resource_group = !local.core_admission_enabled ? [] : [{
    coveredResources = ["cpu", "memory"]
    flavors = [{
      name = local.core_flavor_name
      resources = [
        { name = "cpu", nominalQuota = "0m" },
        { name = "memory", nominalQuota = "0Mi" },
      ]
    }]
  }]
  required_service_classes = toset([
    "platform-critical",
    "presentation",
    "interactive",
    "customer-batch",
    "bulk-backfill",
  ])
  queue_pool_order = {
    for queue_name, queue in local.cluster_queues : queue_name => (
      length(queue.flavor_order) == 0 ? local.pool_ids : queue.flavor_order
    )
  }
  nominal_by_pool = {
    for pool_id, pool in var.pools : pool_id => sum([
      for queue in values(local.cluster_queues) : try(queue.pool_quotas[pool_id].nominal_quota, 0)
    ])
  }
  shared_by_pool = {
    for pool_id, pool in var.pools : pool_id => pool.capacity - local.nominal_by_pool[pool_id]
  }

  service_priority_class_groups = {
    for service_class, policy in var.scheduling.service_classes :
    policy.workload_priority_class => policy.priority...
  }
  service_priority_classes = {
    for priority_name, priorities in local.service_priority_class_groups :
    priority_name => priorities[0]
  }
  priority_classes = merge(var.base_priority_classes, local.service_priority_classes)
  overlapping_priority_classes = setintersection(
    toset(keys(var.base_priority_classes)),
    toset(keys(local.service_priority_classes)),
  )
  local_queue_routes = {
    for queue_name, queue in local.local_queues : queue_name => {
      namespace       = queue.namespace
      cluster_queue   = queue.cluster_queue
      model_ids       = sort(tolist(queue.model_ids))
      tenant_ids      = sort(tolist(queue.tenant_ids))
      service_classes = sort(tolist(queue.service_classes))
    }
  }
  # Resolution rank is explicit: an exact tenant+model+class route wins, then a
  # wildcard-tenant model+class route, then the service class's default lane.
  # Two routes at the SAME rank are a configuration error; an exact route
  # deliberately overlapping a wildcard route is the supported way to give one
  # tenant its own lane, so the keys keep the ranks separate.
  exact_lane_binding_keys = flatten([
    for queue in values(local.local_queues) : flatten([
      for service_class in queue.service_classes : [
        for model_id in queue.model_ids : [
          for tenant_id in sort(tolist(queue.tenant_ids)) :
          jsonencode([service_class, tenant_id, model_id])
        ]
      ]
    ])
  ])
  wildcard_lane_binding_keys = flatten([
    for queue in values(local.local_queues) : flatten([
      for service_class in queue.service_classes : [
        for model_id in queue.model_ids :
        jsonencode([service_class, model_id]) if length(queue.tenant_ids) == 0
      ]
    ])
  ])

  # Lanes that can actually receive work on each ClusterQueue: an explicitly
  # routed lane, or a lane named as a service class default.
  serving_lanes = {
    for queue_name, queue in local.cluster_queues : queue_name => sort(distinct(concat(
      [
        for lane_name, lane in local.local_queues : lane_name
        if lane.cluster_queue == queue_name && length(lane.service_classes) > 0
      ],
      [
        for service_class, policy in local.service_class_contract : policy.default_local_queue
        if try(local.local_queues[policy.default_local_queue].cluster_queue, null) == queue_name
      ],
    )))
  }
  # Kueue 0.17.8 orders pending Workloads inside a ClusterQueue by LocalQueue
  # fair-share usage BEFORE WorkloadPriorityClass when UsageBasedAdmissionFair-
  # Sharing is active (pkg/cache/queue/cluster_queue.go queueOrderingFunc).
  # Priority is therefore decisive only within a single LocalQueue.
  priority_precedence = {
    for queue_name, queue in local.cluster_queues : queue_name => (
      queue.admission_fair_sharing ? "localqueue-fair-share-then-priority" : "priority-then-creation-timestamp"
    )
  }
  fair_share_precedence_required = {
    for queue_name, queue in local.cluster_queues : queue_name => (
      queue.admission_fair_sharing && length(local.serving_lanes[queue_name]) > 1
    )
  }

  cohort_resource_groups = [

    for resource_name in local.resource_names : {
      coveredResources = [resource_name]
      flavors = [
        for pool_id in local.pool_ids : {
          name = var.pools[pool_id].flavor_name
          resources = [{
            name         = resource_name
            nominalQuota = tostring(local.shared_by_pool[pool_id])
          }]
        } if var.pools[pool_id].resource_name == resource_name && local.shared_by_pool[pool_id] > 0
      ]
      } if length([
        for pool_id in local.pool_ids : pool_id
        if var.pools[pool_id].resource_name == resource_name && local.shared_by_pool[pool_id] > 0
    ]) > 0
  ]

  # The Cohort holds the core capacity no queue reserved, which is what makes a
  # zero-floor queue able to admit borrowed or scale-from-zero work that still
  # requests cpu and memory.
  cohort_core_resource_group = !local.core_admission_enabled ? [] : [{
    coveredResources = ["cpu", "memory"]
    flavors = [{
      name = local.core_flavor_name
      resources = [
        { name = "cpu", nominalQuota = "${local.core_shared_quota.cpu_millicores}m" },
        { name = "memory", nominalQuota = "${local.core_shared_quota.memory_mib}Mi" },
      ]
    }]
  }]

  cohort_manifest = !var.scheduling.cohort.enabled ? null : {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "Cohort"
    metadata = {
      name        = var.scheduling.cohort.name
      labels      = var.labels
      annotations = var.annotations
    }
    spec = merge(
      {
        fairSharing = { weight = tostring(var.scheduling.cohort.fair_sharing_weight) }
      },
      length(concat(local.cohort_resource_groups, local.cohort_core_resource_group)) == 0 ? {} : {
        resourceGroups = concat(local.cohort_resource_groups, local.cohort_core_resource_group)
      },
    )
  }

  cluster_queue_manifests = {
    for queue_name, queue in local.cluster_queues : queue_name => {
      apiVersion = "kueue.x-k8s.io/v1beta2"
      kind       = "ClusterQueue"
      metadata = {
        name   = queue_name
        labels = var.labels
        annotations = merge(var.annotations, {
          "fs2-serve.nebius.ai/accelerator-pool-ids" = join(",", local.queue_pool_order[queue_name])
          "fs2-serve.nebius.ai/admitted-namespaces"  = join(",", local.cluster_queue_namespaces[queue_name])
          "fs2-serve.nebius.ai/priority-precedence"  = local.priority_precedence[queue_name]
        })
      }
      spec = merge(
        {
          # matchExpressions rather than matchLabels: one ClusterQueue must be
          # able to admit both the model namespace and a licensed-asset
          # namespace whose PersistentVolumeClaim cannot be mounted elsewhere.
          namespaceSelector = {
            matchExpressions = [{
              key      = "kubernetes.io/metadata.name"
              operator = "In"
              values   = local.cluster_queue_namespaces[queue_name]
            }]
          }
          queueingStrategy = queue.queueing_strategy
          admissionScope = {
            admissionMode = queue.admission_fair_sharing ? "UsageBasedAdmissionFairSharing" : "NoAdmissionFairSharing"
          }
          fairSharing = { weight = tostring(queue.fair_sharing_weight) }
          flavorFungibility = merge(
            {
              whenCanBorrow  = queue.flavor_fungibility.when_can_borrow
              whenCanPreempt = queue.flavor_fungibility.when_can_preempt
            },
            try(queue.flavor_fungibility.preference, null) == null ? {} : {
              # Kueue 0.17.8's v1beta2 CRD permits preference only when both
              # search directions use TryNextFlavor. The precondition below
              # rejects every other combination before it reaches the API.
              preference = queue.flavor_fungibility.preference
            },
          )
          preemption = {
            reclaimWithinCohort = queue.preemption.reclaim_within_cohort
            withinClusterQueue  = queue.preemption.within_cluster_queue
          }
          resourceGroups = concat([
            for resource_name in local.resource_names : {
              coveredResources = [resource_name]
              flavors = [
                for pool_id in local.queue_pool_order[queue_name] : {
                  name = var.pools[pool_id].flavor_name
                  resources = [merge(
                    {
                      name         = resource_name
                      nominalQuota = tostring(try(queue.pool_quotas[pool_id].nominal_quota, 0))
                    },
                    try(queue.pool_quotas[pool_id].borrowing_limit, null) == null ? {} : {
                      borrowingLimit = tostring(queue.pool_quotas[pool_id].borrowing_limit)
                    },
                    try(queue.pool_quotas[pool_id].lending_limit, null) == null ? {} : {
                      lendingLimit = tostring(queue.pool_quotas[pool_id].lending_limit)
                    },
                  )]
                } if var.pools[pool_id].resource_name == resource_name
              ]
            }
            ],
            !local.core_admission_enabled ? [] : [{
              coveredResources = ["cpu", "memory"]
              flavors = [{
                name = local.core_flavor_name
                resources = [
                  {
                    name         = "cpu"
                    nominalQuota = "${queue.core_quota.cpu_millicores}m"
                  },
                  {
                    name         = "memory"
                    nominalQuota = "${queue.core_quota.memory_mib}Mi"
                  },
                ]
              }]
            }],
          )
          stopPolicy = "None"
        },
        var.scheduling.cohort.enabled ? { cohortName = var.scheduling.cohort.name } : {},
        length(queue.admission_checks) == 0 ? {} : {
          admissionChecksStrategy = {
            admissionChecks = [
              for check in queue.admission_checks : {
                name      = check.name
                onFlavors = [for pool_id in check.on_flavors : var.pools[pool_id].flavor_name]
              }
            ]
          }
        },
      )
    }
  }

  local_queue_manifests = {
    for queue_name, queue in local.local_queues : queue_name => {
      apiVersion = "kueue.x-k8s.io/v1beta2"
      kind       = "LocalQueue"
      metadata = {
        name      = queue_name
        namespace = queue.namespace
        labels    = var.labels
        annotations = merge(var.annotations, {
          "fs2-serve.nebius.ai/model-lane-count"   = tostring(length(queue.model_ids))
          "fs2-serve.nebius.ai/model-lane-sha256"  = sha256(jsonencode(sort(tolist(queue.model_ids))))
          "fs2-serve.nebius.ai/tenant-lane-count"  = tostring(length(queue.tenant_ids))
          "fs2-serve.nebius.ai/tenant-lane-sha256" = sha256(jsonencode(sort(tolist(queue.tenant_ids))))
        })
      }
      spec = {
        clusterQueue = queue.cluster_queue
        fairSharing  = { weight = tostring(queue.fair_sharing_weight) }
      }
    }
  }

  priority_class_manifests = {
    for priority_name, priority in local.priority_classes : priority_name => {
      apiVersion = "kueue.x-k8s.io/v1beta2"
      kind       = "WorkloadPriorityClass"
      metadata = {
        name        = priority_name
        labels      = var.labels
        annotations = var.annotations
      }
      value       = priority
      description = "FS2 inference ${priority_name} workload priority"
    }
  }

  service_class_contract = {
    for service_class, policy in var.scheduling.service_classes : service_class => {
      workload_priority_class = policy.workload_priority_class
      priority                = policy.priority
      default_local_queue     = coalesce(try(policy.default_local_queue, null), var.default_queue.local_queue_name)
      preemption_mode         = policy.preemption_mode
      max_queue_seconds       = try(policy.max_queue_seconds, null)
      max_execution_seconds   = try(policy.max_execution_seconds, null)
      caller_selectable       = try(local.service_class_defaults[service_class].caller_selectable, false)
      description = coalesce(
        try(policy.description, null),
        try(local.service_class_defaults[service_class].description, null),
        "Unsupported service class",
      )
      pool_preference = (
        length(policy.pool_preference) == 0 ? local.pool_ids : policy.pool_preference
      )
    }
  }
  # Terraform's length() counts characters; Kubernetes and Kueue bound bytes.
  # Base64 is exact: 3 bytes per 4 characters, minus the padding characters.
  service_class_description_bytes = {
    for service_class, policy in local.service_class_contract : service_class => (
      length(base64encode(policy.description)) / 4 * 3
      -length(regexall("=", base64encode(policy.description)))
    )
  }

  # Kubernetes bounds a ConfigMap in bytes. Base64 is exact: 3 bytes per 4
  # characters minus the padding, where length() would count characters and
  # under-count every non-ASCII description or annotation.
  contract_bytes = (
    length(base64encode(jsonencode(local.contract))) / 4 * 3
    -length(regexall("=", base64encode(jsonencode(local.contract))))
  )

  contract = {
    schema                        = "fs2-serve.nebius.ai/kueue-scheduling/v1"
    scientific_workload_namespace = var.default_queue.namespace
    # Canonical accelerator pool identity. It is rendered onto ResourceFlavor
    # metadata and onto pool node labels, and it is the only key a consumer may
    # use to map an admitted ResourceFlavor back to a pool.
    pool_node_label_key = "accelerator.fs2.nebius/pool-id"
    # Whether Kueue actually counts cpu and memory. While it is "excluded",
    # any cpu/memory nominalQuota in the cluster is inert and must not be
    # described as enforced fairness.
    # Whether Kueue counts cpu and memory at all. excludeResourcePrefixes is
    # one global setting and pkg/workload filterResource drops an excluded
    # request before admission, so while cpu and memory are excluded no
    # cpu/memory nominalQuota in this cluster is enforced.
    core_resource_admission = local.core_admission_enabled ? "budgeted" : "excluded-not-budgeted"
    core_resource_flavor    = local.core_resource_flavor
    core_capacity           = var.core_capacity
    core_shared_quota       = local.core_admission_enabled ? local.core_shared_quota : null
    core_queue_quotas = {
      for queue_name, queue in local.cluster_queues : queue_name => queue.core_quota
      if local.core_admission_enabled
    }
    cpu_stage_requests       = var.cpu_stage_requests
    cohort                   = local.cohort_manifest
    cluster_queues           = local.cluster_queue_manifests
    cluster_queue_namespaces = local.cluster_queue_namespaces
    # ClusterQueues another owner renders, which a lane here may reference.
    external_cluster_queue_names = sort(keys(var.external_cluster_queues))
    external_cluster_queue_quotas = {
      for queue_name, queue in var.external_cluster_queues : queue_name => queue.core_quota
      if queue.core_quota != null
    }
    local_queues = local.local_queue_manifests
    # Names in local_queues that another Terraform owner creates. A caller must
    # not render a manifest for these.
    external_local_queue_names = sort(keys(local.external_local_queues))
    workload_priority_classes  = local.priority_class_manifests
    service_classes            = local.service_class_contract
    local_queue_routes         = local.local_queue_routes
    cluster_queue_pool_order   = local.queue_pool_order
    # Models whose runtime assets exist in exactly one namespace. A consumer
    # must refuse rather than silently fall back to the default namespace,
    # where the licensed claim cannot be mounted.
    namespace_bound_models = var.namespace_bound_models
    priority_precedence    = local.priority_precedence
    # CPU-only stages get no accelerator flavor and therefore no node routing.
    # A consumer must apply this selector and toleration to a CPU PodSet, and
    # must refuse a stage larger than the advertised schedulable capacity.
    cpu_classes = var.cpu_classes
    # Whether a CPU-only stage can run at all. With no CPU class a model that
    # needs a raw data stage must be given enriched inputs instead.
    cpu_stage_execution = length(var.cpu_classes) == 0 ? "enriched-inputs-only" : "available"
    pools = {
      for pool_id, pool in var.pools : pool_id => {
        resource_flavor           = pool.flavor_name
        accelerator_resource_name = pool.resource_name
        capacity                  = pool.capacity
      }
    }
    resource_flavor_pool_ids = { for pool_id, pool in var.pools : pool.flavor_name => pool_id }
    pool_capacity            = { for pool_id, pool in var.pools : pool_id => pool.capacity }
    shared_pool_quota        = local.shared_by_pool
  }
}

resource "terraform_data" "contract" {
  input = local.contract

  lifecycle {
    precondition {
      condition     = local.contract_bytes <= 900000
      error_message = "The rendered scheduling contract must remain below 900,000 UTF-8 bytes so the immutable Kubernetes ConfigMap has safe metadata headroom."
    }

    precondition {
      condition = (
        length(var.pools) <= 32 &&
        length(local.resource_names) <= 16 &&
        length(distinct([for pool in values(var.pools) : pool.flavor_name])) == length(var.pools) &&
        alltrue([
          for resource_name in local.resource_names : length([
            for pool in values(var.pools) : pool if pool.resource_name == resource_name
          ]) <= 64
        ])
      )
      error_message = "Pool flavors must be unique and remain within the application's 32 selected-pool bound and Kueue's 16 resource-group and 64 flavors-per-group CRD limits."
    }

    precondition {
      condition = (
        !var.scheduling.cohort.enabled ||
        (
          length(var.scheduling.cohort.name) <= 63 &&
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.scheduling.cohort.name))
        )
      ) && var.scheduling.cohort.fair_sharing_weight > 0.000000001
      error_message = "The cohort name must be a DNS subdomain and its fair-sharing weight must be greater than Kueue's 1e-9 webhook floor."
    }

    precondition {
      condition = alltrue([
        for queue_name, queue in local.cluster_queues :
        # The durable consumer DTO bounds a persisted ClusterQueue identity
        # at 63 characters, so a longer name is refused here even though the
        # Kubernetes API would accept it.
        length(queue_name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue_name)) &&
        alltrue([
          for namespace in local.cluster_queue_namespaces[queue_name] :
          length(namespace) <= 63 && can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace))
        ]) &&
        length(local.cluster_queue_namespaces[queue_name]) <= 32 &&
        contains(["BestEffortFIFO", "StrictFIFO"], queue.queueing_strategy) &&
        queue.fair_sharing_weight > 0.000000001 &&
        contains(["Never", "LowerPriority", "Any"], queue.preemption.reclaim_within_cohort) &&
        contains(["Never", "LowerPriority", "LowerOrNewerEqualPriority"], queue.preemption.within_cluster_queue) &&
        contains(["MayStopSearch", "TryNextFlavor"], queue.flavor_fungibility.when_can_borrow) &&
        contains(["MayStopSearch", "TryNextFlavor"], queue.flavor_fungibility.when_can_preempt) &&
        (
          try(queue.flavor_fungibility.preference, null) == null ||
          (
            queue.flavor_fungibility.when_can_borrow == "TryNextFlavor" &&
            queue.flavor_fungibility.when_can_preempt == "TryNextFlavor" &&
            contains(
              ["BorrowingOverPreemption", "PreemptionOverBorrowing"],
              queue.flavor_fungibility.preference,
            )
          )
        ) &&
        toset(local.queue_pool_order[queue_name]) == toset(local.pool_ids) &&
        length(local.queue_pool_order[queue_name]) == length(distinct(local.queue_pool_order[queue_name])) &&
        length(setsubtract(toset(keys(queue.pool_quotas)), toset(local.pool_ids))) == 0 &&
        length(queue.admission_checks) <= 64 &&
        length(queue.admission_checks) == length(distinct([for check in queue.admission_checks : check.name])) &&
        alltrue([
          for check in queue.admission_checks :
          length(check.name) <= 63 &&
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", check.name)) &&
          length(check.on_flavors) <= 64 &&
          length(check.on_flavors) == length(distinct(check.on_flavors)) &&
          length(setsubtract(toset(check.on_flavors), toset(local.pool_ids))) == 0
        ])
      ])
      error_message = "Every ClusterQueue must have valid names/policies, at most 32 label-safe admitted namespaces, at most 64 admission checks and flavors per check, a duplicate-free complete pool order, and a flavor preference only when both search directions are TryNextFlavor."
    }

    precondition {
      condition = alltrue(flatten([
        for queue in values(local.cluster_queues) : [
          for pool_id, quota in queue.pool_quotas :
          floor(quota.nominal_quota) == quota.nominal_quota && quota.nominal_quota >= 0 &&
          (try(quota.borrowing_limit, null) == null || (
            var.scheduling.cohort.enabled &&
            floor(quota.borrowing_limit) == quota.borrowing_limit && quota.borrowing_limit >= 0
          )) &&
          (try(quota.lending_limit, null) == null || (
            var.scheduling.cohort.enabled &&
            floor(quota.lending_limit) == quota.lending_limit && quota.lending_limit >= 0 &&
            quota.lending_limit <= quota.nominal_quota
          ))
        ]
        ])) && alltrue([
        for pool_id, shared in local.shared_by_pool : shared >= 0
      ])
      error_message = "Pool quotas and limits must be nonnegative whole accelerator counts; lending cannot exceed nominal quota, limits require a cohort, and total nominal floors cannot exceed physical/max-autoscaled pool capacity."
    }

    # Without a Cohort there is nowhere for unassigned capacity to be borrowed
    # from, so any residual quota is unusable. Require exact equality instead of
    # silently stranding accelerators.
    precondition {
      condition = var.scheduling.cohort.enabled || alltrue([
        for pool_id, shared in local.shared_by_pool : shared == 0
      ])
      error_message = "With the shared Cohort disabled, ClusterQueue nominal floors must equal pool capacity exactly; residual quota would be unreachable because no queue could borrow it."
    }

    precondition {
      condition = (
        alltrue([
          for queue_name, queue in local.local_queues :
          length(queue_name) <= 63 &&
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue_name)) &&
          (
            queue_name != var.default_queue.local_queue_name ||
            (
              queue.cluster_queue == var.default_queue.cluster_queue_name &&
              queue.namespace == var.default_queue.namespace
            )
          ) &&
          contains(local.referenceable_cluster_queues, queue.cluster_queue) &&
          contains(local.cluster_queue_namespaces[queue.cluster_queue], queue.namespace) &&
          queue.fair_sharing_weight > 0.000000001 &&
          alltrue([
            for model_id in queue.model_ids :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", model_id))
          ]) &&
          alltrue([
            for tenant_id in queue.tenant_ids :
            length(tenant_id) <= 63 && can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", tenant_id))
          ]) &&
          length(setsubtract(queue.service_classes, local.required_service_classes)) == 0 &&
          (
            length(queue.model_ids) == 0 ? length(queue.service_classes) == 0 : length(queue.service_classes) > 0
          ) &&
          (length(queue.tenant_ids) == 0 || length(queue.model_ids) > 0) &&
          alltrue([
            # Pool order and preemption policy belong to the owner that
            # renders the ClusterQueue. A lane on an externally owned queue
            # (the reference-data CPU queue) is checked for identity and
            # namespace admission only.
            # A conditional, not a boolean or: an externally owned ClusterQueue
            # has no pool order or preemption policy here, and indexing it
            # would fail rather than skip.
            for service_class in queue.service_classes :
            !contains(keys(local.cluster_queues), queue.cluster_queue) ? true : try(
              (
                length(var.scheduling.service_classes[service_class].pool_preference) == 0 ? local.pool_ids :
                var.scheduling.service_classes[service_class].pool_preference
              ) == local.queue_pool_order[queue.cluster_queue] &&
              (
                !contains(local.high_priority_service_classes, service_class) || (
                  contains(
                    ["LowerPriority", "Any"],
                    local.cluster_queues[queue.cluster_queue].preemption.reclaim_within_cohort,
                    ) && contains(
                    ["LowerPriority", "LowerOrNewerEqualPriority"],
                    local.cluster_queues[queue.cluster_queue].preemption.within_cluster_queue,
                  )
                )
              ),
              false,
            )
          ])
        ]) &&
        length(local.exact_lane_binding_keys) == length(distinct(local.exact_lane_binding_keys)) &&
        length(local.wildcard_lane_binding_keys) == length(distinct(local.wildcard_lane_binding_keys))
      )
      error_message = "Every LocalQueue and tenant label identity must be at most 63 characters and label-safe, live in a namespace its ClusterQueue admits, reference an existing ClusterQueue, use a positive fair-sharing weight, and have explicit service-class/tenant/model lane bindings whose pool order matches the selected ClusterQueue; two routes of the same rank cannot claim one service-class/tenant/model triple, and dead routes and stable LocalQueue rebinding are forbidden."
    }

    # A model bound to one namespace must have a lane there for every class its
    # callers can select, otherwise a presentation request silently resolves to
    # the default namespace where its licensed claim cannot be mounted.
    precondition {
      condition = (
        length(setintersection(
          toset(keys(local.external_local_queues)),
          toset(keys(local.managed_local_queues)),
        )) == 0
      )
      error_message = "A LocalQueue name is claimed both by this module and by an external owner; exactly one Terraform owner may create each queue."
    }

    precondition {
      condition = alltrue([
        for model_id, namespace in var.namespace_bound_models :
        length(model_id) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", model_id)) &&
        length(namespace) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace)) &&
        length(setsubtract(
          local.caller_selectable_service_classes,
          toset(flatten([
            for queue in values(local.local_queues) : tolist(queue.service_classes)
            if queue.namespace == namespace && contains(queue.model_ids, model_id)
          ])),
        )) == 0
      ])
      error_message = "A namespace-bound model must have a lane in its own namespace for every caller-selectable service class; otherwise a caller-selected class would resolve to a namespace without its licensed assets."
    }

    # Exact quota math: declared floors plus the Cohort residual equal the
    # exact totals, and nothing exceeds them.
    precondition {
      condition = local.core_admission_enabled ? (
        floor(var.core_capacity.cpu_millicores) == var.core_capacity.cpu_millicores &&
        var.core_capacity.cpu_millicores >= 1 &&
        floor(var.core_capacity.memory_mib) == var.core_capacity.memory_mib &&
        var.core_capacity.memory_mib >= 1 &&
        length(var.core_capacity.flavor_name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.core_capacity.flavor_name)) &&
        !contains([for pool in values(var.pools) : pool.flavor_name], var.core_capacity.flavor_name) &&
        alltrue([
          for queue_name, queue in local.cluster_queues :
          floor(queue.core_quota.cpu_millicores) == queue.core_quota.cpu_millicores &&
          queue.core_quota.cpu_millicores >= 0 &&
          floor(queue.core_quota.memory_mib) == queue.core_quota.memory_mib &&
          queue.core_quota.memory_mib >= 0
        ]) &&
        local.core_floor_totals.cpu_millicores <= var.core_capacity.cpu_millicores &&
        local.core_floor_totals.memory_mib <= var.core_capacity.memory_mib &&
        local.core_shared_quota.cpu_millicores >= 0 &&
        local.core_shared_quota.memory_mib >= 0 &&
        local.core_floor_totals.cpu_millicores + local.core_shared_quota.cpu_millicores ==
        var.core_capacity.cpu_millicores &&
        local.core_floor_totals.memory_mib + local.core_shared_quota.memory_mib ==
        var.core_capacity.memory_mib &&
        # Residual core capacity is only reachable through a Cohort, so a
        # zero-floor queue could otherwise never admit core-requesting work.
        (
          var.scheduling.cohort.enabled || (
            local.core_shared_quota.cpu_millicores == 0 &&
            local.core_shared_quota.memory_mib == 0
          )
        )
        ) : alltrue([
          for queue_name, queue in local.cluster_queues :
          queue.core_quota.cpu_millicores == 0 && queue.core_quota.memory_mib == 0
      ])
      error_message = "Core admission requires exact positive aggregate cpu/memory totals, a distinct label-less core ResourceFlavor name, whole nonnegative per-queue floors whose sum plus the Cohort residual equals those totals exactly, and a Cohort whenever any residual remains; without core_capacity no ClusterQueue may declare a core floor, because Kueue drops the request before admission and the quota would be inert."
    }

    # One Pod must fit one node, whatever quota its queue holds.
    precondition {
      condition = alltrue([
        for class_name, request in var.cpu_stage_requests :
        contains(keys(var.cpu_classes), class_name) &&
        floor(request.cpu_millicores) == request.cpu_millicores && request.cpu_millicores >= 1 &&
        floor(request.memory_mib) == request.memory_mib && request.memory_mib >= 1 &&
        try(var.cpu_classes[class_name].schedulable_capacity, null) != null &&
        # One Pod must fit one node...
        request.cpu_millicores <= var.cpu_classes[class_name].schedulable_capacity.cpu_millicores &&
        request.memory_mib <= var.cpu_classes[class_name].schedulable_capacity.memory_mib &&
        # ...and the queue that admits it must hold at least that much quota.
        # For an externally owned ClusterQueue that quota comes from its owner.
        (
          contains(keys(var.external_cluster_queues), var.cpu_classes[class_name].cluster_queue)
          ? try(
            var.external_cluster_queues[var.cpu_classes[class_name].cluster_queue].core_quota.cpu_millicores >= request.cpu_millicores &&
            var.external_cluster_queues[var.cpu_classes[class_name].cluster_queue].core_quota.memory_mib >= request.memory_mib,
            false,
          )
          : try(
            local.core_admission_enabled &&
            local.cluster_queues[var.cpu_classes[class_name].cluster_queue].core_quota.cpu_millicores >= request.cpu_millicores &&
            local.cluster_queues[var.cpu_classes[class_name].cluster_queue].core_quota.memory_mib >= request.memory_mib,
            false,
          )
        )
      ])
      error_message = "Every declared CPU stage request must name a configured CPU class that advertises a per-node schedulable capacity, must fit inside that per-node capacity, and must fit inside the core quota of the ClusterQueue that admits it; an externally owned queue must publish its own core quota here. A pool whose nodes are smaller than the stage, or a queue whose quota is smaller than one Pod, cannot run it."
    }

    precondition {
      condition = alltrue([
        for class_name, placement in var.cpu_classes :
        length(class_name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", class_name)) &&
        length(placement.local_queue) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", placement.local_queue)) &&
        length(placement.cluster_queue) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", placement.cluster_queue)) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", placement.namespace)) &&
        # The class must resolve to a queue that exists and admits its own
        # namespace, whether this module or another owner renders it.
        contains(keys(local.local_queues), placement.local_queue) &&
        local.local_queues[placement.local_queue].namespace == placement.namespace &&
        local.local_queues[placement.local_queue].cluster_queue == placement.cluster_queue &&
        contains(local.cluster_queue_namespaces[placement.cluster_queue], placement.namespace) &&
        (placement.pool_id == null || (
          length(placement.pool_id) <= 63 &&
          can(regex("^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$", placement.pool_id))
        )) &&
        length(placement.node_selector) <= 8 &&
        alltrue([
          for key, value in placement.node_selector :
          length(key) <= 317 &&
          can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key)) &&
          length(value) <= 63 &&
          can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", value))
        ]) &&
        length(placement.tolerations) <= 8 &&
        alltrue([
          for toleration in placement.tolerations :
          contains(["Equal", "Exists"], toleration.operator) &&
          contains(["NoSchedule", "PreferNoSchedule", "NoExecute"], toleration.effect) &&
          length(toleration.key) <= 253 &&
          (toleration.operator == "Exists" ? toleration.value == null : (
            toleration.value != null && length(toleration.value) <= 63
          ))
        ]) &&
        # A toleration without a selector would let the stage land anywhere,
        # and a tainted pool without a toleration is unschedulable.
        (length(placement.tolerations) == 0 || length(placement.node_selector) >= 1) &&
        (placement.schedulable_capacity == null || (
          placement.schedulable_capacity.cpu_millicores >= 1 &&
          placement.schedulable_capacity.memory_mib >= 1 &&
          placement.schedulable_capacity.ephemeral_storage_mib >= 0
        ))
      ])
      error_message = "Every CPU class must name an existing LocalQueue in its own namespace on a ClusterQueue that admits that namespace, keep every persisted identity within 63 characters, use at most 8 label-safe selector entries and 8 valid tolerations, pair any toleration with a node selector, and advertise a positive per-node schedulable capacity when it declares one."
    }

    precondition {
      condition = toset(keys(var.scheduling.service_classes)) == local.required_service_classes && alltrue([
        for service_class, policy in local.service_class_contract :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", service_class)) &&
        length(policy.workload_priority_class) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", policy.workload_priority_class)) &&
        floor(policy.priority) == policy.priority && policy.priority >= -2147483648 && policy.priority <= 2147483647 &&
        policy.preemption_mode == "restartable" &&
        local.service_class_description_bytes[service_class] >= 1 &&
        local.service_class_description_bytes[service_class] <= 500 &&
        contains(keys(local.local_queues), policy.default_local_queue) &&
        (policy.max_queue_seconds == null || (
          floor(policy.max_queue_seconds) == policy.max_queue_seconds &&
          policy.max_queue_seconds >= 1 &&
          policy.max_queue_seconds <= 2147483647
        )) &&
        (policy.max_execution_seconds == null || (
          floor(policy.max_execution_seconds) == policy.max_execution_seconds &&
          policy.max_execution_seconds >= 1 &&
          policy.max_execution_seconds <= 2147483647
        )) &&
        toset(policy.pool_preference) == toset(local.pool_ids) &&
        length(policy.pool_preference) == length(distinct(policy.pool_preference)) &&
        try(
          policy.pool_preference == local.queue_pool_order[local.local_queues[policy.default_local_queue].cluster_queue],
          false,
        ) &&
        (
          !contains(local.high_priority_service_classes, service_class) || try(
            contains(
              ["LowerPriority", "Any"],
              local.cluster_queues[local.local_queues[policy.default_local_queue].cluster_queue].preemption.reclaim_within_cohort,
              ) && contains(
              ["LowerPriority", "LowerOrNewerEqualPriority"],
              local.cluster_queues[local.local_queues[policy.default_local_queue].cluster_queue].preemption.within_cluster_queue,
            ),
            false,
          )
        )
      ])
      error_message = "Scheduling must define exactly the five supported service classes with valid priority, queue, restartable-only execution, signed-int32 queue/execution SLA, a 1-500 byte description, and a pool order exactly matching the selected ClusterQueue; platform-critical/presentation defaults must support both cross-ClusterQueue reclaim and same-ClusterQueue priority displacement. Non-preemptible and checkpointable execution remain blocked until a separate enforcement/handshake contract exists."
    }

    precondition {
      condition = try(
        local.service_class_contract["platform-critical"].priority > local.service_class_contract.presentation.priority &&
        local.service_class_contract.presentation.priority > local.service_class_contract.interactive.priority &&
        local.service_class_contract.interactive.priority > local.service_class_contract["customer-batch"].priority &&
        local.service_class_contract["customer-batch"].priority > local.service_class_contract["bulk-backfill"].priority,
        false,
      )
      error_message = "Service-class priorities must be strictly ordered: platform-critical > presentation > interactive > customer-batch > bulk-backfill."
    }

    # Kueue's usage-based admission fair sharing orders LocalQueues by decayed
    # usage before it compares WorkloadPriorityClass, so a presentation lane
    # does not categorically precede a bulk lane in a different LocalQueue.
    # Make the operator choose that tradeoff explicitly rather than inferring a
    # priority guarantee the scheduler does not provide.
    precondition {
      condition = alltrue([
        for queue_name, queue in local.cluster_queues :
        !local.fair_share_precedence_required[queue_name] || queue.fair_share_precedence_acknowledged
      ])
      error_message = "A ClusterQueue serving more than one LocalQueue with UsageBasedAdmissionFairSharing orders pending work by LocalQueue fair-share usage before WorkloadPriorityClass, so higher service classes in a different LocalQueue are not categorically admitted first. Either set admission_fair_sharing = false on that ClusterQueue, route the competing classes through one LocalQueue, or set fair_share_precedence_acknowledged = true to accept fair-share ordering."
    }

    precondition {
      condition = alltrue([
        for priorities in values(local.service_priority_class_groups) :
        length(distinct(priorities)) == 1
        ]) && alltrue([
        for priority_name in local.overlapping_priority_classes :
        var.base_priority_classes[priority_name] == local.service_priority_classes[priority_name]
      ])
      error_message = "Service classes sharing a WorkloadPriorityClass must agree on its value and cannot redefine an existing class with a different value."
    }
  }
}
