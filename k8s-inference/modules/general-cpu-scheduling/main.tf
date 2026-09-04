# General CPU admission lane.
#
# One ResourceFlavor, one ClusterQueue that budgets cpu and memory, and one
# LocalQueue for the ordinary general-cpu class. The ClusterQueue may admit an
# additional stack-owned namespace whose distinct LocalQueue and class are
# assembled elsewhere; this module owns exactly its three objects and no more.
#
# It contributes one thing to scheduling: the canonical general-cpu class entry
# and its digest. The scheduling workstream remains the sole producer of the
# reference-data class, the sole assembler of the contract, and the sole owner
# of the ConfigMap that carries it. This module never merges another owner's
# class, never assembles a document and never creates that ConfigMap.
#
# The lane joins no cohort. That is deliberate: cohort membership is how Kueue
# lends and borrows, and neither direction is wanted here. Borrowing would let
# general aggregation consume the reference-database capacity that AlphaFold 3
# raw preprocessing depends on; lending would let accelerator work drain the
# CPU pool that exists so aggregation never waits behind a GPU queue.

locals {
  enabled  = var.lane.enabled && length(var.pool_contract.pools) > 0
  pool_ids = sort(keys(var.pool_contract.pools))

  # Nominal quota is measured per-node capacity times the maximum node count an
  # operator authorized. A scale-from-zero pool still contributes its envelope,
  # so a Job admits and triggers the autoscaler rather than waiting for a node
  # that only appears once something is admitted.
  lane_cpu_millicores = sum(concat([0], [
    for pool in values(var.pool_contract.pools) :
    pool.schedulable_capacity.cpu_millicores * pool.max_nodes
  ]))
  lane_memory_mib = sum(concat([0], [
    for pool in values(var.pool_contract.pools) :
    pool.schedulable_capacity.memory_mib * pool.max_nodes
  ]))
  lane_ephemeral_mib = sum(concat([0], [
    for pool in values(var.pool_contract.pools) :
    pool.schedulable_capacity.ephemeral_storage_mib * pool.max_nodes
  ]))

  # A pod runs on one node, so the largest single node is what a per-pod request
  # must fit. That, not the lane total, is the class's schedulable_capacity.
  largest_node = {
    cpu_millicores = max(0, [
      for pool in values(var.pool_contract.pools) : pool.schedulable_capacity.cpu_millicores
    ]...)
    memory_mib = max(0, [
      for pool in values(var.pool_contract.pools) : pool.schedulable_capacity.memory_mib
    ]...)
    ephemeral_storage_mib = max(0, [
      for pool in values(var.pool_contract.pools) : pool.schedulable_capacity.ephemeral_storage_mib
    ]...)
  }

  nominal_cpu    = "${local.lane_cpu_millicores}m"
  nominal_memory = "${local.lane_memory_mib}Mi"

  tolerations = [{
    key      = var.pool_contract.taint.key
    operator = "Equal"
    value    = var.pool_contract.taint.value
    effect   = var.pool_contract.taint.effect
  }]

  # Every class remains single-namespace, but a ClusterQueue may admit several
  # classes through distinct LocalQueue names. The primary general-cpu class
  # keeps the configured namespace; the workloads assembler may contribute an
  # academic-cpu class for the licensed namespace while reusing this exact
  # flavor, quota, and pool.
  execution_namespace = local.enabled ? var.lane.namespace : null
  admitted_namespaces = local.enabled ? sort(distinct(concat(
    [var.lane.namespace],
    tolist(var.lane.admitted_namespaces),
  ))) : []

  # v1 binds one class to exactly one pool. Kueue reports the flavor it admitted
  # through, so a flavor that spans several pools could not tell a consumer
  # which node group actually ran the stage, and the class could not name a
  # pool_id. Rather than claim a binding Kueue cannot make, the lane is single
  # pool and its flavor selector carries that pool's own identity label.
  class_pool_id = local.enabled ? local.pool_ids[0] : null
  flavor_node_selector = merge(var.pool_contract.node_selector, {
    "capacity.fs2.nebius/pool-id" = local.class_pool_id
  })

  # Capacity is published both as Kubernetes quantities, which a controller
  # persists and compares directly against a pod request, and as the raw
  # measured numbers, so nothing downstream has to re-parse a quantity string.
  class_capacity = {
    cpu                   = "${local.largest_node.cpu_millicores}m"
    memory                = "${local.largest_node.memory_mib}Mi"
    ephemeral_storage     = "${local.largest_node.ephemeral_storage_mib}Mi"
    cpu_millicores        = local.largest_node.cpu_millicores
    memory_mib            = local.largest_node.memory_mib
    ephemeral_storage_mib = local.largest_node.ephemeral_storage_mib
  }

  general_cpu_class = !local.enabled ? null : {
    local_queue     = var.lane.local_queue
    cluster_queue   = var.lane.cluster_queue
    namespace       = local.execution_namespace
    resource_flavor = var.lane.resource_flavor
    # How a consumer learns which node group actually ran the stage, and the
    # only place the actual pool appears. This flavor's selector pins exactly
    # one pool, so the flavor Kueue admits through is itself the answer and no
    # Node read is required.
    # Exactly the keys this mode defines. The published class schema forbids
    # the key a mode does not use, and the assembler normalizes to the same
    # shape before it emits and digests the entry, so this digest and the
    # published one are digests of identical bytes.
    pool_resolution = {
      mode    = "per-pool-flavor"
      pool_id = local.class_pool_id
    }
    node_selector = local.flavor_node_selector
    tolerations   = local.tolerations
    # Scheduling is authoritative about which pools may run the class. A
    # consumer enforces this list; it never widens placement from whatever a
    # runtime happens to report as compatible.
    eligible_pool_ids    = [local.class_pool_id]
    schedulable_capacity = local.class_capacity
  }

  # The single class entry this module contributes. The scheduling owner
  # assembles it together with the classes it produces itself.
  cpu_classes = local.enabled ? { "general-cpu" = local.general_cpu_class } : {}
  cpu_class_digests = {
    for class_id, entry in local.cpu_classes : class_id => sha256(jsonencode(entry))
  }

  resource_flavor_manifest = !local.enabled ? null : {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name = var.lane.resource_flavor
      labels = merge(var.labels, {
        "capacity.fs2.nebius/pool" = "general-cpu"
      })
      annotations = merge(var.annotations, {
        "fs2-serve.nebius.ai/general-cpu-pool-ids" = join(",", local.pool_ids)
      })
    }
    spec = {
      nodeLabels  = local.flavor_node_selector
      tolerations = local.tolerations
    }
  }

  cluster_queue_manifest = !local.enabled ? null : {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata = {
      name   = var.lane.cluster_queue
      labels = var.labels
      annotations = merge(var.annotations, {
        "fs2-serve.nebius.ai/general-cpu-pool-ids" = join(",", local.pool_ids)
      })
    }
    spec = {
      namespaceSelector = {
        matchExpressions = [{
          key      = "kubernetes.io/metadata.name"
          operator = "In"
          values   = local.admitted_namespaces
        }]
      }
      queueingStrategy = var.lane.queueing_strategy
      # Without this, withinClusterQueue defaults to Never and a presentation
      # or interactive CPU stage waits behind admitted bulk work on the only
      # lane that can run it, whatever WorkloadPriorityClass it carries. This
      # queue is outside the accelerator Cohort, so in-queue displacement is
      # the only mechanism it has.
      preemption = {
        reclaimWithinCohort = "Never"
        withinClusterQueue  = "LowerPriority"
      }
      fairSharing = { weight = tostring(var.lane.fair_sharing_weight) }
      resourceGroups = [{
        coveredResources = ["cpu", "memory"]
        flavors = [{
          name = var.lane.resource_flavor
          resources = [
            { name = "cpu", nominalQuota = local.nominal_cpu },
            { name = "memory", nominalQuota = local.nominal_memory },
          ]
        }]
      }]
      stopPolicy = "None"
    }
  }

  local_queue_manifests = !local.enabled ? {} : {
    (var.lane.namespace) = {
      apiVersion = "kueue.x-k8s.io/v1beta2"
      kind       = "LocalQueue"
      metadata = {
        name        = var.lane.local_queue
        namespace   = var.lane.namespace
        labels      = var.labels
        annotations = var.annotations
      }
      spec = {
        clusterQueue = var.lane.cluster_queue
        fairSharing  = { weight = tostring(var.lane.fair_sharing_weight) }
      }
    }
  }

  contract = {
    cpu_classes_schema = "fs2-serve.nebius.ai/cpu-stage-classes/v1"
    enabled            = local.enabled
    # Exactly the canonical shape, so the assembler merges rather than adapts.
    cpu_classes       = local.cpu_classes
    cpu_class_digests = local.cpu_class_digests
    capacity = {
      cpu_millicores        = local.lane_cpu_millicores
      memory_mib            = local.lane_memory_mib
      ephemeral_storage_mib = local.lane_ephemeral_mib
      largest_node          = local.largest_node
      nominal_cpu           = local.nominal_cpu
      nominal_memory        = local.nominal_memory
    }
    pool_ids            = local.pool_ids
    execution_namespace = local.execution_namespace
    admitted_namespaces = local.admitted_namespaces
    # The Kueue objects this producer owns, so the assembler can describe them
    # without ever becoming their owner.
    external_lane_facts = !local.enabled ? null : {
      cluster_queue  = var.lane.cluster_queue
      local_queue    = var.lane.local_queue
      namespace      = local.execution_namespace
      namespaces     = local.admitted_namespaces
      nominal_cpu    = local.nominal_cpu
      nominal_memory = local.nominal_memory
      owner          = "modules/general-cpu-scheduling"
    }
    elastic         = anytrue(concat([false], [for pool in values(var.pool_contract.pools) : pool.elastic]))
    scale_from_zero = anytrue(concat([false], [for pool in values(var.pool_contract.pools) : pool.scale_from_zero]))
    manifests = {
      resource_flavor = local.resource_flavor_manifest
      cluster_queue   = local.cluster_queue_manifest
      local_queues    = local.local_queue_manifests
    }
    cohort = null
  }
}

resource "terraform_data" "contract" {
  input = local.contract

  lifecycle {
    precondition {
      condition = !local.enabled || alltrue([
        for name in [var.lane.cluster_queue, var.lane.local_queue, var.lane.resource_flavor] :
        can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name)) && length(name) <= 63
      ])
      error_message = "The general CPU lane must use DNS-label queue and flavor names."
    }

    precondition {
      condition = !local.enabled || alltrue([
        for namespace in local.admitted_namespaces :
        namespace != null &&
        can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", namespace)) &&
        length(namespace) <= 63
      ])
      error_message = "The general CPU lane needs DNS-label admitted namespaces; a ClusterQueue whose namespace selector matches nothing would never admit."
    }

    # Every entry must carry the shared required fields, so a controller reading
    # either class finds the same keys in the same shape.
    precondition {
      condition = alltrue([
        for class_id, entry in local.cpu_classes : length(setsubtract(
          toset(["local_queue", "cluster_queue", "namespace", "resource_flavor", "pool_resolution", "node_selector", "tolerations", "eligible_pool_ids", "schedulable_capacity"]),
          toset(keys(entry)),
          )) == 0 && length(setsubtract(
          toset(keys(entry)),
          toset(["local_queue", "cluster_queue", "namespace", "resource_flavor", "pool_resolution", "node_selector", "tolerations", "eligible_pool_ids", "schedulable_capacity"]),
        )) == 0 && length(entry.eligible_pool_ids) > 0 &&
        contains(["per-pool-flavor", "node-label-observation"], entry.pool_resolution.mode) && (
          entry.pool_resolution.mode == "per-pool-flavor" ?
          contains(entry.eligible_pool_ids, entry.pool_resolution.pool_id) :
          try(length(entry.pool_resolution.node_label_key), 0) > 0
        ) &&
        # The quantity strings and the raw numbers describe one capacity, so
        # they must agree exactly. A mismatch would let a consumer size a Pod
        # from one field and check it against the other.
        entry.schedulable_capacity.cpu == "${entry.schedulable_capacity.cpu_millicores}m" &&
        entry.schedulable_capacity.memory == "${entry.schedulable_capacity.memory_mib}Mi" &&
        entry.schedulable_capacity.ephemeral_storage == "${entry.schedulable_capacity.ephemeral_storage_mib}Mi"
      ])
      error_message = "Every cpu_classes entry must carry exactly the canonical fields: local_queue, cluster_queue, namespace, resource_flavor, a pool_resolution object naming per-pool-flavor with one of its own eligible pools or node-label-observation with a label key, node_selector, tolerations, eligible_pool_ids, and schedulable_capacity whose cpu, memory and ephemeral_storage quantities equal exactly their measured cpu_millicores, memory_mib and ephemeral_storage_mib."
    }

    # Ownership separation is the reason this lane exists, so it is a hard gate
    # rather than a naming convention.
    precondition {
      condition = !local.enabled || var.reference_data_lane == null || (
        local.cpu_classes["general-cpu"].cluster_queue != var.reference_data_lane.cluster_queue &&
        local.cpu_classes["general-cpu"].resource_flavor != var.reference_data_lane.resource_flavor &&
        !contains(local.admitted_namespaces, var.reference_data_lane.namespace)
      )
      error_message = "The general CPU lane must not reuse the reference-data ClusterQueue, ResourceFlavor or admit its namespace; reference-database capacity is never lent to general aggregation."
    }

    precondition {
      condition = !local.enabled || (
        local.lane_cpu_millicores > 0 &&
        local.lane_memory_mib > 0 &&
        local.largest_node.cpu_millicores > 0 &&
        local.largest_node.memory_mib > 0
      )
      error_message = "A general CPU lane with zero nominal quota would admit nothing; every pool must contribute measured schedulable capacity."
    }

    precondition {
      condition = !local.enabled || alltrue([
        for pool_id, pool in var.pool_contract.pools :
        pool.max_nodes >= 1 && pool.max_nodes >= pool.min_nodes
      ])
      error_message = "Every general CPU pool must authorize at least one node and a maximum no smaller than its minimum."
    }

    # One class, one flavor, one pool. Kueue surfaces the flavor it admitted
    # through, so this is what lets a consumer persist the actual pool.
    precondition {
      condition     = !local.enabled || length(local.pool_ids) == 1
      error_message = "The v1 general CPU lane binds exactly one pool, because one shared ResourceFlavor across several pools cannot tell a consumer which node group actually ran a stage. Declare one pool, or extend the contract with namespace- and pool-qualified class identities end to end first."
    }

    precondition {
      condition = !local.enabled || (
        local.flavor_node_selector["capacity.fs2.nebius/pool-id"] == local.class_pool_id &&
        local.cpu_classes["general-cpu"].pool_resolution.mode == "per-pool-flavor" &&
        local.cpu_classes["general-cpu"].pool_resolution.pool_id == local.class_pool_id &&
        local.cpu_classes["general-cpu"].eligible_pool_ids == [local.class_pool_id]
      )
      error_message = "The rendered flavor selector, class pool_id and eligible pools must all name the same actual pool."
    }

  }
}
