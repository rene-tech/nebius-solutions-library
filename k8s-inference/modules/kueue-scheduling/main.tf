locals {
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
    queueing_strategy      = var.default_queue.queueing_strategy
    fair_sharing_weight    = 1
    admission_fair_sharing = true
    flavor_order           = sort(keys(var.pools))
    pool_quotas            = local.default_pool_quotas
    preemption = {
      reclaim_within_cohort = "Never"
      within_cluster_queue  = "Never"
    }
  }
  default_local_queue = {
    namespace           = var.default_queue.namespace
    cluster_queue       = var.default_queue.cluster_queue_name
    fair_sharing_weight = 1
    model_ids           = toset([])
  }

  # The stable default objects always remain in the topology. An explicit entry
  # with the same name changes their policy without changing their Terraform or
  # Kubernetes identity, so already-admitted serving workloads survive rollout.
  cluster_queues = merge(
    { (var.default_queue.cluster_queue_name) = local.default_cluster_queue },
    var.scheduling.cluster_queues,
  )
  local_queues = merge(
    { (var.default_queue.local_queue_name) = local.default_local_queue },
    var.scheduling.local_queues,
  )

  pool_ids       = sort(keys(var.pools))
  resource_names = sort(distinct([for pool in values(var.pools) : pool.resource_name]))
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
      length(local.cohort_resource_groups) == 0 ? {} : {
        resourceGroups = local.cohort_resource_groups
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
        })
      }
      spec = merge(
        {
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = queue.namespace }
          }
          queueingStrategy = queue.queueing_strategy
          admissionScope = {
            admissionMode = queue.admission_fair_sharing ? "UsageBasedAdmissionFairSharing" : "NoAdmissionFairSharing"
          }
          fairSharing = { weight = tostring(queue.fair_sharing_weight) }
          preemption = {
            reclaimWithinCohort = queue.preemption.reclaim_within_cohort
            withinClusterQueue  = queue.preemption.within_cluster_queue
          }
          resourceGroups = [
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
          ]
          stopPolicy = "None"
        },
        var.scheduling.cohort.enabled ? { cohortName = var.scheduling.cohort.name } : {},
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
          "fs2-serve.nebius.ai/model-lane-count"  = tostring(length(queue.model_ids))
          "fs2-serve.nebius.ai/model-lane-sha256" = sha256(jsonencode(sort(tolist(queue.model_ids))))
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
      default_local_queue     = try(policy.default_local_queue, null)
      preemption_mode         = policy.preemption_mode
      pool_preference = (
        length(policy.pool_preference) == 0 ? local.pool_ids : policy.pool_preference
      )
    }
  }

  contract = {
    schema                    = "fs2-serve.nebius.ai/kueue-scheduling/v1"
    cohort                    = local.cohort_manifest
    cluster_queues            = local.cluster_queue_manifests
    local_queues              = local.local_queue_manifests
    workload_priority_classes = local.priority_class_manifests
    service_classes           = local.service_class_contract
    pool_capacity             = { for pool_id, pool in var.pools : pool_id => pool.capacity }
    shared_pool_quota         = local.shared_by_pool
  }
}

resource "terraform_data" "contract" {
  input = local.contract

  lifecycle {
    precondition {
      condition = (
        length(var.pools) <= 256 &&
        length(local.resource_names) <= 16 &&
        length(distinct([for pool in values(var.pools) : pool.flavor_name])) == length(var.pools) &&
        alltrue([
          for resource_name in local.resource_names : length([
            for pool in values(var.pools) : pool if pool.resource_name == resource_name
          ]) <= 64
        ])
      )
      error_message = "Pool flavors must be unique and remain within Kueue's 16 resource-group, 64 flavors-per-group, and 256 total-flavor limits."
    }

    precondition {
      condition = (
        !var.scheduling.cohort.enabled ||
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", var.scheduling.cohort.name))
      ) && var.scheduling.cohort.fair_sharing_weight > 0
      error_message = "The cohort name must be a DNS subdomain and its fair-sharing weight must be positive."
    }

    precondition {
      condition = alltrue([
        for queue_name, queue in local.cluster_queues :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", queue_name)) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue.namespace)) &&
        contains(["BestEffortFIFO", "StrictFIFO"], queue.queueing_strategy) &&
        queue.fair_sharing_weight > 0 &&
        contains(["Never", "LowerPriority", "Any"], queue.preemption.reclaim_within_cohort) &&
        contains(["Never", "LowerPriority", "LowerOrNewerEqualPriority"], queue.preemption.within_cluster_queue) &&
        toset(local.queue_pool_order[queue_name]) == toset(local.pool_ids) &&
        length(local.queue_pool_order[queue_name]) == length(distinct(local.queue_pool_order[queue_name])) &&
        length(setsubtract(toset(keys(queue.pool_quotas)), toset(local.pool_ids))) == 0
      ])
      error_message = "Every ClusterQueue must have valid names/policies and a duplicate-free flavor order containing every configured accelerator pool."
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

    precondition {
      condition = alltrue([
        for queue_name, queue in local.local_queues :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", queue_name)) &&
        contains(keys(local.cluster_queues), queue.cluster_queue) &&
        queue.namespace == local.cluster_queues[queue.cluster_queue].namespace &&
        queue.fair_sharing_weight > 0
      ])
      error_message = "Every LocalQueue must reference a ClusterQueue in the same namespace and use a positive fair-sharing weight."
    }

    precondition {
      condition = length(setsubtract(
        local.required_service_classes,
        toset(keys(var.scheduling.service_classes)),
        )) == 0 && alltrue([
        for service_class, policy in local.service_class_contract :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", service_class)) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", policy.workload_priority_class)) &&
        floor(policy.priority) == policy.priority && policy.priority >= -2147483648 && policy.priority <= 2147483647 &&
        contains(["non-preemptible", "restartable", "checkpointable"], policy.preemption_mode) &&
        (policy.default_local_queue == null || contains(keys(local.local_queues), policy.default_local_queue)) &&
        toset(policy.pool_preference) == toset(local.pool_ids) &&
        length(policy.pool_preference) == length(distinct(policy.pool_preference))
      ])
      error_message = "Scheduling must define all five required service classes with valid priority, queue, preemption, and complete duplicate-free pool-preference contracts."
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
