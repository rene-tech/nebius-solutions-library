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
  # Classes whose work must not sit behind admitted bulk. Interactive belongs
  # here: numeric WorkloadPriorityClass ordering is decisive only inside one
  # LocalQueue, and with usage-based admission fair sharing it is not decisive
  # even there against a lane with less decayed usage. Nothing but reclaim and
  # in-queue displacement can take capacity back from bulk work that is
  # already admitted, so a lane serving these classes must have both.
  high_priority_service_classes = ["platform-critical", "presentation", "interactive"]
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
    flavor_order = (
      length(var.default_queue.flavor_order) == 0
      ? local.capacity_ordered_pool_ids
      : var.default_queue.flavor_order
    )
    flavor_fungibility = {
      when_can_borrow  = "MayStopSearch"
      when_can_preempt = "TryNextFlavor"
      preference       = null
    }
    admission_checks                   = []
    pool_quotas                        = local.default_pool_quotas
    fair_share_precedence_acknowledged = var.default_queue.fair_share_precedence_acknowledged
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

  pool_ids = sort(keys(var.pools))
  # Alphabetical pool IDs say nothing about which capacity should be used
  # first, and a pool named for burst capacity often sorts ahead of the warm
  # pool beside it. The
  # default order is derived from the facts instead: capacity that is always
  # there before capacity that can be reclaimed, and a pool with a node floor
  # before one that must scale up, each group alphabetical so the result is
  # stable. An operator may still state an order explicitly.
  capacity_ordered_pool_ids = concat(
    sort([for pool_id, pool in var.pools : pool_id if !pool.preemptible && pool.min_nodes > 0]),
    sort([for pool_id, pool in var.pools : pool_id if !pool.preemptible && pool.min_nodes == 0]),
    sort([for pool_id, pool in var.pools : pool_id if pool.preemptible && pool.min_nodes > 0]),
    sort([for pool_id, pool in var.pools : pool_id if pool.preemptible && pool.min_nodes == 0]),
  )
  # Rank the four tiers above so an explicit order can choose between equally
  # stable pools without ever sending work to reclaimable or scale-from-zero
  # capacity while a warmer tier is still available.
  pool_stability_tier = {
    for pool_id, pool in var.pools : pool_id => (
      !pool.preemptible && pool.min_nodes > 0 ? 0 :
      !pool.preemptible ? 1 :
      pool.min_nodes > 0 ? 2 : 3
    )
  }
  resource_names = sort(distinct([for pool in values(var.pools) : pool.resource_name]))

  # Core admission is pool-coupled. cpu and memory join the accelerator
  # resourceGroup rather than forming one of their own, because Kueue assigns
  # exactly one ResourceFlavor per resourceGroup per PodSet: with all three
  # resources in one group, the cpu and memory a Workload reserves necessarily
  # come from the pool whose accelerators it reserved. A separate label-less
  # core flavor cannot make that promise, and aggregate core accounting lets a
  # Workload hold accelerators in one pool against core capacity measured on
  # another and then fit no node at all.
  #
  # The cost of the coupling is that a ClusterQueue can carry only one
  # accelerator resource name once core admission is on, because a resource
  # belongs to exactly one resourceGroup. That case is refused rather than
  # silently decoupled.
  core_admission_enabled = length(var.core_capacity) > 0
  # Each queue's share of a pool's core capacity is the same share of that
  # pool it reserved in accelerators. A queue with no accelerator floor in a
  # pool gets no core floor there either and borrows both from the Cohort
  # together, which keeps the two in step.
  core_queue_pool_quota = {
    for queue_name, queue in local.cluster_queues : queue_name => {
      for pool_id in local.pool_ids : pool_id => {
        cpu_millicores = (
          !local.core_admission_enabled || var.pools[pool_id].capacity == 0 ? 0 : floor(
            try(var.core_capacity[pool_id].cpu_millicores, 0) *
            try(queue.pool_quotas[pool_id].nominal_quota, 0) / var.pools[pool_id].capacity
          )
        )
        memory_mib = (
          !local.core_admission_enabled || var.pools[pool_id].capacity == 0 ? 0 : floor(
            try(var.core_capacity[pool_id].memory_mib, 0) *
            try(queue.pool_quotas[pool_id].nominal_quota, 0) / var.pools[pool_id].capacity
          )
        )
      }
    }
  }
  core_shared_by_pool = {
    for pool_id in local.pool_ids : pool_id => {
      cpu_millicores = !local.core_admission_enabled ? 0 : (
        try(var.core_capacity[pool_id].cpu_millicores, 0) - sum(concat([0], [
          for queue_name in keys(local.cluster_queues) :
          local.core_queue_pool_quota[queue_name][pool_id].cpu_millicores
        ]))
      )
      memory_mib = !local.core_admission_enabled ? 0 : (
        try(var.core_capacity[pool_id].memory_mib, 0) - sum(concat([0], [
          for queue_name in keys(local.cluster_queues) :
          local.core_queue_pool_quota[queue_name][pool_id].memory_mib
        ]))
      )
    }
  }
  required_service_classes = toset([
    "platform-critical",
    "presentation",
    "interactive",
    "customer-batch",
    "bulk-backfill",
  ])
  # An explicit order is the operator's; an empty one resolves to the same
  # capacity-derived order the stable queue uses, never to alphabetical pool
  # IDs, which say nothing about which capacity should be searched first.
  queue_pool_order = {
    for queue_name, queue in local.cluster_queues : queue_name => (
      length(queue.flavor_order) == 0 ? local.capacity_ordered_pool_ids : queue.flavor_order
    )
  }
  queue_pool_order_is_warm_first = {
    for queue_name, order in local.queue_pool_order : queue_name => alltrue([
      for index, pool_id in order : index == 0 ? true : try(
        local.pool_stability_tier[order[index - 1]] <= local.pool_stability_tier[pool_id],
        false,
      )
    ])
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
  # Which ClusterQueues serve a protected class, and which serve only lower
  # classes while holding a nominal floor of their own.
  #
  # Kueue 0.17.8's reclaimWithinCohort preempts only Workloads a queue is
  # running ABOVE its nominal quota, that is, capacity it borrowed. Work
  # inside another ClusterQueue's own floor cannot be reclaimed cross-queue at
  # any priority. So a bulk queue with a real floor can hold that capacity
  # against a presentation or interactive lane in a different queue, and no
  # WorkloadPriorityClass changes that. Only two things make displacement
  # possible: the two classes sharing one ClusterQueue, where
  # withinClusterQueue preemption applies, or the lower-priority queue holding
  # no floor at all, so everything it runs is borrowed and reclaimable.
  protected_serving_cluster_queues = sort([
    for queue_name in keys(local.cluster_queues) : queue_name
    if length(setintersection(
      toset(local.high_priority_service_classes),
      toset(flatten([
        for lane_name in local.serving_lanes[queue_name] :
        tolist(try(local.local_queues[lane_name].service_classes, []))
      ])),
    )) > 0
  ])
  unreclaimable_lower_priority_queues = sort([
    for queue_name, queue in local.cluster_queues : queue_name
    if !contains(local.protected_serving_cluster_queues, queue_name) &&
    length(local.serving_lanes[queue_name]) > 0 &&
    sum(concat([0], [for quota in values(queue.pool_quotas) : quota.nominal_quota])) > 0
  ])

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

  # The Cohort holds what no queue reserved, per pool and per resource, so a
  # zero-floor queue can borrow accelerators and the core capacity that sits
  # on the same nodes together rather than one without the other.
  cohort_resource_groups = [
    for resource_name in local.resource_names : {
      coveredResources = concat(
        [resource_name],
        local.core_admission_enabled ? ["cpu", "memory"] : [],
      )
      flavors = [
        for pool_id in local.pool_ids : {
          name = var.pools[pool_id].flavor_name
          resources = concat(
            [{
              name         = resource_name
              nominalQuota = tostring(local.shared_by_pool[pool_id])
            }],
            !local.core_admission_enabled ? [] : [
              {
                name         = "cpu"
                nominalQuota = "${local.core_shared_by_pool[pool_id].cpu_millicores}m"
              },
              {
                name         = "memory"
                nominalQuota = "${local.core_shared_by_pool[pool_id].memory_mib}Mi"
              },
            ],
          )
          } if var.pools[pool_id].resource_name == resource_name && (
          local.shared_by_pool[pool_id] > 0 || local.core_admission_enabled
        )
      ]
      } if length([
        for pool_id in local.pool_ids : pool_id
        if var.pools[pool_id].resource_name == resource_name && (
          local.shared_by_pool[pool_id] > 0 || local.core_admission_enabled
        )
    ]) > 0
  ]
  cohort_core_resource_group = []

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
          # cpu and memory ride in the accelerator group so Kueue's single
          # flavor assignment per group ties them to one pool.
          resourceGroups = [
            for resource_name in local.resource_names : {
              coveredResources = concat(
                [resource_name],
                local.core_admission_enabled ? ["cpu", "memory"] : [],
              )
              flavors = [
                for pool_id in local.queue_pool_order[queue_name] : {
                  name = var.pools[pool_id].flavor_name
                  resources = concat(
                    [merge(
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
                    )],
                    !local.core_admission_enabled ? [] : [
                      {
                        name         = "cpu"
                        nominalQuota = "${local.core_queue_pool_quota[queue_name][pool_id].cpu_millicores}m"
                      },
                      {
                        name         = "memory"
                        nominalQuota = "${local.core_queue_pool_quota[queue_name][pool_id].memory_mib}Mi"
                      },
                    ],
                  )
                } if var.pools[pool_id].resource_name == resource_name
              ]
            }
          ]
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

  # An unset pool_preference inherits the search order of the ClusterQueue the
  # class actually routes to, not alphabetical pool ID order. Terraform
  # requires a class and its queue to agree, so the alphabetical default made
  # one operator decision ("search warm capacity first") into six identical
  # settings, and any queue order other than alphabetical failed validation
  # until all of them were repeated. This reads the queue instead.
  #
  # It resolves the queue without local.local_queues, which is derived from
  # the service classes; going through it would be a cycle.
  service_class_default_cluster_queues = {
    for service_class, policy in var.scheduling.service_classes : service_class => (
      coalesce(try(policy.default_local_queue, null), var.default_queue.local_queue_name) ==
      var.default_queue.local_queue_name
      ? var.default_queue.cluster_queue_name
      : try(
        var.scheduling.local_queues[
          coalesce(try(policy.default_local_queue, null), var.default_queue.local_queue_name)
        ].cluster_queue,
        null,
      )
    )
  }
  service_class_inherited_pool_order = {
    for service_class, cluster_queue in local.service_class_default_cluster_queues :
    service_class => try(local.queue_pool_order[cluster_queue], local.pool_ids)
  }

  # Terraform fills an absent optional attribute with null, and the published
  # class schema forbids the key a mode does not use rather than accepting a
  # null. Rebuild each entry with exactly the keys its mode defines, so the
  # bytes in the ConfigMap validate and a contributor's digest of the same
  # normalized entry matches the one published beside it.
  published_cpu_classes = {
    for class_name, class in var.cpu_classes : class_name => {
      local_queue       = class.local_queue
      cluster_queue     = class.cluster_queue
      namespace         = class.namespace
      resource_flavor   = class.resource_flavor
      eligible_pool_ids = class.eligible_pool_ids
      pool_resolution = class.pool_resolution.mode == "per-pool-flavor" ? {
        mode    = class.pool_resolution.mode
        pool_id = class.pool_resolution.pool_id
        } : {
        mode           = class.pool_resolution.mode
        node_label_key = class.pool_resolution.node_label_key
      }
      node_selector = class.node_selector
      tolerations = [
        for toleration in class.tolerations :
        toleration.operator == "Exists" ? {
          key      = toleration.key
          operator = toleration.operator
          effect   = toleration.effect
          } : {
          key      = toleration.key
          operator = toleration.operator
          value    = toleration.value
          effect   = toleration.effect
        }
      ]
      schedulable_capacity = class.schedulable_capacity
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
        length(policy.pool_preference) == 0
        ? local.service_class_inherited_pool_order[service_class]
        : policy.pool_preference
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
    # Pool-coupled: cpu and memory are budgeted on the accelerator pool's own
    # ResourceFlavor, so a consumer reads a Workload's core reservation from
    # the same flavor that granted its accelerators. There is no separate core
    # flavor to look up and no aggregate to reconcile.
    core_resource_flavor = null
    core_capacity        = var.core_capacity
    core_shared_quota    = local.core_admission_enabled ? local.core_shared_by_pool : null
    core_queue_quotas = {
      for queue_name in keys(local.cluster_queues) :
      queue_name => local.core_queue_pool_quota[queue_name]
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
    # The CPU classes carry their own version, so a consumer reading these
    # exact bytes knows which class contract they conform to instead of
    # inferring it from the scheduling schema. Additive: a v1 consumer that
    # does not know the key ignores it.
    cpu_classes_schema = "fs2-serve.nebius.ai/cpu-stage-classes/v1"
    cpu_classes        = local.published_cpu_classes
    # One digest per contributed class, over that entry alone. The ConfigMap
    # digest covers the whole policy, so it changes whenever anything does; a
    # consumer that froze one class needs to know whether that class changed,
    # and a contributor needs to confirm the assembler published its entry
    # unaltered. Terraform sorts object keys in jsonencode, so the digest is
    # stable for an unchanged entry.
    cpu_class_digests = {
      for class_name, class in local.published_cpu_classes :
      class_name => sha256(jsonencode(class))
    }
    # Whether a CPU-only stage can run at all. With no CPU class a model that
    # needs a raw data stage must be given enriched inputs instead.
    cpu_stage_execution = length(var.cpu_classes) == 0 ? "enriched-inputs-only" : "available"
    # Deterministic eligibility, so a consumer never guesses from a pool name.
    # An empty intersection with a service class's pool order is a refusal, and
    # a model or stage absent from these maps has no lane in this deployment.
    model_eligible_pool_ids = var.model_eligible_pool_ids
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
        # The CRD caps a ClusterQueue and a Cohort at 16 resourceGroups, and
        # core admission adds one more beside the accelerator groups.
        # One resourceGroup per accelerator resource. cpu and memory join an
        # existing group rather than adding one, so core admission no longer
        # consumes a slot.
        length(local.resource_names) <= 16 &&
        length(distinct([for pool in values(var.pools) : pool.flavor_name])) == length(var.pools) &&
        alltrue([
          for resource_name in local.resource_names : length([
            for pool in values(var.pools) : pool if pool.resource_name == resource_name
          ]) <= 64
        ])
      )
      error_message = "Pool flavors must be unique and remain within the application's 32 selected-pool bound and Kueue's 16 resource-group and 64 flavors-per-group CRD limits; with core admission on, the shared core group occupies one of those sixteen."
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

    # Cross-ClusterQueue displacement is not a consequence of priority. Kueue
    # 0.17.8 reclaims only what another queue borrowed above its nominal
    # quota, so a bulk queue with a floor holds that capacity against a
    # presentation or interactive lane elsewhere no matter how the priorities
    # compare. Either the classes share one ClusterQueue, where
    # withinClusterQueue preemption applies, or the lower-priority queue holds
    # no floor so everything it runs is borrowed and therefore reclaimable, or
    # the operator says plainly that they accept the arrangement.
    precondition {
      condition = (
        !var.scheduling.cohort.enabled ||
        length(local.protected_serving_cluster_queues) == 0 ||
        length(local.unreclaimable_lower_priority_queues) == 0
      )
      error_message = "A protected class (${join(", ", local.high_priority_service_classes)}) is served by ${join(", ", local.protected_serving_cluster_queues)}, while ${join(", ", local.unreclaimable_lower_priority_queues)} serves only lower classes and holds a nominal floor. Kueue reclaims only capacity another queue borrowed above its nominal quota, so work inside that floor cannot be displaced at any priority and presentation or interactive work would wait behind it. There are exactly two topologies that work: route the protected and bulk classes through one ClusterQueue, where withinClusterQueue preemption applies, or give the lower-priority queue a zero nominal floor so everything it runs is borrowed and therefore reclaimable."
    }

    # A CPU stage requests cpu and memory and nothing else. While core
    # admission is off, Kueue's excludeResourcePrefixes drop both before
    # admission, so every cpu/memory quota in the cluster is inert and the
    # class would appear admissible while budgeting nothing. With the Cohort
    # disabled the queue cannot borrow either, so it needs a real floor of its
    # own. Refused here, before any stage creates a pool or a queue.
    precondition {
      condition = length(var.cpu_classes) == 0 || (
        local.core_admission_enabled &&
        alltrue([
          for class_name, class in var.cpu_classes :
          # A CPU stage class runs on a CPU ClusterQueue, which another owner
          # creates and which is outside the accelerator Cohort. It cannot
          # borrow, so it needs a real cpu and memory floor of its own.
          try(var.external_cluster_queues[class.cluster_queue].core_quota.cpu_millicores, 0) >= 1 &&
          try(var.external_cluster_queues[class.cluster_queue].core_quota.memory_mib, 0) >= 1
        ])
      )
      error_message = "A CPU stage class requires core-resource admission: without core_capacity, Kueue excludes cpu and memory before admission and every cpu/memory quota is inert. With the Cohort disabled the class's ClusterQueue must also declare a positive cpu and memory floor of its own, because it cannot borrow one."
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
        local.queue_pool_order_is_warm_first[queue_name] &&
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
      error_message = "Every ClusterQueue must have valid names/policies, at most 32 label-safe admitted namespaces, at most 64 admission checks and flavors per check, a duplicate-free complete warm-first pool order, and a flavor preference only when both search directions are TryNextFlavor. An explicit order may reorder equally stable pools, but may not put preemptible or scale-from-zero capacity ahead of a warmer tier."
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
              # The resolved preference, so an unset one inherits its own
              # queue's order and only a real disagreement fails.
              local.service_class_contract[service_class].pool_preference ==
              local.queue_pool_order[queue.cluster_queue] &&
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

    # Pool-coupled core admission, and the one shape Kueue can guarantee.
    #
    # A resource belongs to exactly one resourceGroup, so cpu and memory can
    # ride with only one accelerator resource name. This release therefore
    # supports core admission for a deployment that advertises exactly one
    # accelerator resource key. Pools may differ in GPU class, node size and
    # capacity; what they must share is the resource name Kueue budgets.
    #
    # This is a stated limitation, not a routing decision to be worked around
    # inside a deployment: the alternative is decoupling cpu and memory from
    # the accelerator, which is the exact failure this design prevents, and
    # per-resource ClusterQueue grouping is not implemented here.
    precondition {
      condition = local.core_admission_enabled ? (
        length(local.resource_names) == 1 &&
        toset(keys(var.core_capacity)) == toset(local.pool_ids) &&
        # Every per-pool share, and the Cohort residual beside it, is whole
        # and nonnegative, and the two add up to exactly what the pool has.
        alltrue([
          for pool_id in local.pool_ids :
          local.core_shared_by_pool[pool_id].cpu_millicores >= 0 &&
          local.core_shared_by_pool[pool_id].memory_mib >= 0 &&
          sum(concat([0], [
            for queue_name in keys(local.cluster_queues) :
            local.core_queue_pool_quota[queue_name][pool_id].cpu_millicores
          ])) + local.core_shared_by_pool[pool_id].cpu_millicores ==
          var.core_capacity[pool_id].cpu_millicores &&
          sum(concat([0], [
            for queue_name in keys(local.cluster_queues) :
            local.core_queue_pool_quota[queue_name][pool_id].memory_mib
          ])) + local.core_shared_by_pool[pool_id].memory_mib ==
          var.core_capacity[pool_id].memory_mib
        ]) &&
        # Residual capacity is only reachable through a Cohort, so without one
        # a zero-floor queue could never admit core-requesting work.
        (
          var.scheduling.cohort.enabled || alltrue([
            for pool_id in local.pool_ids :
            local.core_shared_by_pool[pool_id].cpu_millicores == 0 &&
            local.core_shared_by_pool[pool_id].memory_mib == 0
          ])
        )
      ) : true
      error_message = "Core admission in this release supports exactly one accelerator resource name per deployment, and this contract advertises ${length(local.resource_names)} (${join(", ", local.resource_names)}). cpu and memory share the accelerator resourceGroup so Kueue grants them from the same pool as the accelerators, and a resource belongs to exactly one resourceGroup, so a second accelerator resource cannot be coupled. Pools may differ in GPU class, node size and capacity, but they must advertise the same resource key; otherwise leave core admission off. It also requires measured capacity for every pool, per-pool shares plus the Cohort residual equalling each pool's capacity exactly, and a Cohort whenever any residual remains."
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
          # A CPU class on a queue this module renders would be an
          # accelerator queue, whose core quota is pool-coupled and sized from
          # accelerator shares rather than from a CPU stage request.
          : false
        )
      ])
      error_message = "Every declared CPU stage request must name a configured CPU class that advertises a per-node schedulable capacity, must fit inside that per-node capacity, and must fit inside the core quota of the ClusterQueue that admits it; an externally owned queue must publish its own core quota here. A pool whose nodes are smaller than the stage, or a queue whose quota is smaller than one Pod, cannot run it."
    }

    precondition {
      condition = (
        length(setintersection(
          toset(keys(var.external_cluster_queues)),
          toset(keys(local.cluster_queues)),
        )) == 0 &&
        alltrue([
          for queue_name, queue in var.external_cluster_queues :
          length(queue_name) <= 63 &&
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue_name)) &&
          length(queue.namespaces) >= 1 &&
          length(queue.namespaces) <= 32 &&
          length(queue.namespaces) == length(distinct(queue.namespaces)) &&
          alltrue([
            for namespace in queue.namespaces :
            length(namespace) <= 63 &&
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace))
          ]) &&
          (queue.core_quota == null || (
            floor(queue.core_quota.cpu_millicores) == queue.core_quota.cpu_millicores &&
            queue.core_quota.cpu_millicores >= 0 &&
            floor(queue.core_quota.memory_mib) == queue.core_quota.memory_mib &&
            queue.core_quota.memory_mib >= 0
          ))
        ]) &&
        length(setsubtract(
          toset(keys(var.required_namespaces)),
          toset(local.referenceable_cluster_queues),
        )) == 0 &&
        # The merged result is what the contract publishes, so validate that,
        # not only the inputs that feed it.
        alltrue([
          for queue_name, namespaces in local.cluster_queue_namespaces :
          length(namespaces) >= 1 &&
          length(namespaces) <= 32 &&
          length(namespaces) == length(distinct(namespaces)) &&
          alltrue([
            for namespace in namespaces :
            length(namespace) <= 63 &&
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace))
          ])
        ])
      )
      error_message = "An externally owned ClusterQueue must not share a name with one this module renders, must list 1-32 duplicate-free label-safe namespaces, must declare whole nonnegative core quota when it declares any, and every required-namespace key must name a ClusterQueue that exists here or is declared external. After merging required namespaces, every published ClusterQueue must still admit 1-32 duplicate-free label-safe namespaces."
    }

    precondition {
      condition = alltrue([
        for model_id, pool_ids in var.model_eligible_pool_ids :
        length(model_id) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", model_id)) &&
        length(pool_ids) == length(distinct(pool_ids)) &&
        length(setsubtract(toset(pool_ids), toset(local.pool_ids))) == 0 &&
        # A Workload requests exactly one resource name and Kueue's flavor
        # fallback never crosses a resourceGroup, so a set spanning a full-GPU
        # resource and a MIG-slice resource is not a fallback set.
        length(distinct([
          for pool_id in pool_ids : var.pools[pool_id].resource_name
        ])) <= 1
      ])
      error_message = "Every eligible-pool entry must name a label-safe model and a duplicate-free subset of this deployment's pools that all advertise the same accelerator resource name, because one Kueue Workload requests one resource and its flavor fallback cannot cross a resourceGroup. A selected model with no deployed compatible pool is an explicit empty list, which a consumer refuses; an absent key means the model is unknown here and is refused too."
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
        length(placement.resource_flavor) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", placement.resource_flavor)) &&
        # A CPU class's flavor belongs to its own queue's owner, so it must not
        # collide with an accelerator flavor this module renders.
        !contains([for pool in values(var.pools) : pool.flavor_name], placement.resource_flavor) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", placement.namespace)) &&
        # The class must resolve to a queue that exists and admits its own
        # namespace, whether this module or another owner renders it.
        contains(keys(local.local_queues), placement.local_queue) &&
        local.local_queues[placement.local_queue].namespace == placement.namespace &&
        local.local_queues[placement.local_queue].cluster_queue == placement.cluster_queue &&
        contains(local.cluster_queue_namespaces[placement.cluster_queue], placement.namespace) &&
        # Exactly one owner for the lane. This module assembles classes from
        # several contributors, and a LocalQueue name claimed both here and by
        # an external owner cannot be resolved back to one placement.
        !(
          contains(keys(local.managed_local_queues), placement.local_queue) &&
          contains(keys(local.external_local_queues), placement.local_queue)
        ) &&
        # Expected pools, and how the actual one becomes knowable. A single
        # flavor covering several pools cannot report which one ran the stage,
        # so such a class must name the Node label a consumer reads after
        # scheduling instead of implying an assignment.
        alltrue([
          for pool_id in placement.eligible_pool_ids :
          length(pool_id) <= 63 &&
          can(regex("^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$", pool_id))
        ]) &&
        (
          placement.pool_resolution.mode == "per-pool-flavor"
          ? placement.pool_resolution.pool_id == one(placement.eligible_pool_ids)
          : can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", placement.pool_resolution.node_label_key))
        ) &&
        length(placement.node_selector) <= 16 &&
        alltrue([
          for key, value in placement.node_selector :
          # Each half of a qualified name is bounded separately: 253 before the
          # slash, 63 after. A total-length check alone accepts a 254-character
          # prefix the API rejects.
          length(key) <= 317 &&
          length(split("/", key)) <= 2 &&
          length(split("/", key)[0]) <= (length(split("/", key)) == 2 ? 253 : 63) &&
          (length(split("/", key)) == 1 || length(element(split("/", key), 1)) <= 63) &&
          can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key)) &&
          length(value) <= 63 &&
          can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", value))
        ]) &&
        length(placement.tolerations) <= 8 &&
        alltrue([
          for toleration in placement.tolerations :
          contains(["Equal", "Exists"], toleration.operator) &&
          contains(["NoSchedule", "PreferNoSchedule", "NoExecute"], toleration.effect) &&
          # A toleration key is a Kubernetes qualified name, so it may carry a
          # prefix: at most 253 characters before the slash and 63 after, 317
          # in all. The same bound and grammar as the variable validation and
          # the published class schema.
          length(toleration.key) <= 317 &&
          length(split("/", toleration.key)) <= 2 &&
          length(split("/", toleration.key)[0]) <= (length(split("/", toleration.key)) == 2 ? 253 : 63) &&
          (length(split("/", toleration.key)) == 1 || length(element(split("/", toleration.key), 1)) <= 63) &&
          can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", toleration.key)) &&
          (toleration.operator == "Exists" ? toleration.value == null : (
            toleration.value != null &&
            length(toleration.value) <= 63 &&
            # A value the API would reject is not a toleration, whatever its
            # length: a Kubernetes label value has its own grammar.
            can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", toleration.value))
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
