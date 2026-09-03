locals {
  accelerator_pool_profiles = jsondecode(
    file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")
  )
  custom_accelerator_pool_profile = {
    enabled = true
    state   = "customer-specified"
    pools   = { for pool_id in keys(var.accelerator_pool_contract.pools) : pool_id => {} }
    queue = {
      cluster_queue_name = "inference-accelerators"
      local_queue_name   = "inference-models"
      queueing_strategy  = "BestEffortFIFO"
      acceptance_pool_id = sort(keys(var.accelerator_pool_contract.pools))[0]
    }
  }
  selected_accelerator_pool_profile = var.accelerator_pool_contract.profile == "custom" ? local.custom_accelerator_pool_profile : local.accelerator_pool_profiles.profiles[var.accelerator_pool_contract.profile]
  # Preserve the established queue renderer field path while sourcing every
  # value from the authoritative v2 contract. No template capacity or hardware
  # fact is reintroduced by this compatibility projection.
  selected_queue_pools = {
    for pool_id, pool in var.accelerator_pool_contract.pools : pool_id => merge(pool, {
      accelerator = { resource_api = pool.resource_api }
    })
  }
  # Kueue 0.17.8 limits ResourceFlavor.spec.nodeLabels to eight entries. A
  # flavor only needs the stable accelerator class and unique pool identity;
  # the complete label set remains on the provider node group and model Pods
  # retain their own topology, storage, capacity, and architecture constraints.
  resource_flavor_node_label_keys = toset([
    "accelerator.fs2.nebius/class",
    "accelerator.fs2.nebius/pool-id",
  ])
  resource_flavor_node_labels = {
    for pool_id, pool in local.selected_queue_pools : pool_id => {
      for label in local.resource_flavor_node_label_keys :
      label => pool.scheduling.stable_node_labels[label]
    }
  }
  queue_accelerator_pools = {
    for pool_id, pool in local.selected_queue_pools : pool_id => {
      flavor_name   = pool.scheduling.resource_flavor_name
      resource_name = pool.accelerator.resource_api.resource_name
      capacity      = pool.node.gpus_per_node * pool.capacity.max_nodes
    }
  }
  # A PersistentVolumeClaim is mountable only from its own namespace, so
  # licensed academic work runs in the claim namespace. Every caller-selectable
  # class therefore needs a lane there; otherwise a presentation request for
  # AlphaFold 3 would resolve to fs2-models and lose the claim.
  academic_execution_enabled = var.academic_assets.enabled && var.academic_assets.execution.enabled
  academic_model_ids = sort(distinct([
    for asset in values(var.academic_assets.assets) : asset.model_id
  ]))
  academic_scheduling_local_queues = local.academic_execution_enabled ? {
    (var.academic_assets.execution.local_queue) = {
      namespace           = var.academic_assets.namespace
      cluster_queue       = var.academic_assets.execution.cluster_queue
      fair_sharing_weight = 1
      model_ids           = toset(local.academic_model_ids)
      tenant_ids          = toset([var.academic_assets.tenant_id])
      service_classes = toset([
        "platform-critical",
        "presentation",
        "interactive",
        "customer-batch",
        "bulk-backfill",
      ])
    }
  } : {}
  academic_namespace_bound_models = {
    for model_id in local.academic_model_ids : model_id => var.academic_assets.namespace
    if local.academic_execution_enabled
  }
  # The academic queue object belongs to modules/academic-assets, beside the
  # claim and namespace it serves, so the scheduling owner describes it without
  # creating it. A collision with an operator lane is a real conflict.
  academic_lane_queue_collisions = sort([
    for queue_name in concat(
      keys(local.academic_scheduling_local_queues),
      keys(local.academic_cpu_local_queues),
    ) : queue_name
    if contains(keys(var.scheduling.local_queues), queue_name)
  ])

  # The reference queue's quota is authored in Kubernetes quantity strings.
  # Convert to the contract's exact integer units so a stage request can be
  # compared with it.
  reference_queue_cpu_millicores = var.reference_data.enabled ? (
    endswith(var.reference_data.queue.nominal_cpu, "m")
    ? tonumber(trimsuffix(var.reference_data.queue.nominal_cpu, "m"))
    : tonumber(var.reference_data.queue.nominal_cpu) * 1000
  ) : 0
  # Kueue quantities are authored as strings. Convert the whole supported
  # Ki|Mi|Gi|Ti grammar the root facade accepts, so a valid tfvars value can
  # never reach a narrower parser here, and refuse a value that is not a whole
  # number of MiB because the contract carries integer MiB.
  reference_memory_unit_mib = {
    Ki = 1 / 1024
    Mi = 1
    Gi = 1024
    Ti = 1048576
  }
  reference_memory_unit = var.reference_data.enabled ? substr(
    var.reference_data.queue.nominal_memory,
    length(var.reference_data.queue.nominal_memory) - 2,
    2,
  ) : "Mi"
  reference_memory_amount = var.reference_data.enabled ? tonumber(substr(
    var.reference_data.queue.nominal_memory,
    0,
    length(var.reference_data.queue.nominal_memory) - 2,
  )) : 0
  reference_queue_memory_mib = var.reference_data.enabled ? (
    local.reference_memory_amount * lookup(local.reference_memory_unit_mib, local.reference_memory_unit, 0)
  ) : 0

  # The raw AlphaFold 3 data stage's measured request. Kubernetes allocatable is
  # lower than a machine preset's nominal size, so the pool must advertise at
  # least this much schedulable capacity per node.
  raw_af3_cpu_request = {
    cpu_millicores = 16000
    memory_mib     = 65536
  }

  academic_service_classes = toset([
    "platform-critical",
    "presentation",
    "interactive",
    "customer-batch",
    "bulk-backfill",
  ])
  # A licensed model's CPU data stage reads the shared reference databases and
  # belongs on the tainted reference-data pool, which is a different
  # ClusterQueue from the accelerator lane. Both lanes live in the claim
  # namespace so either stage keeps its private volume, and a consumer freezes
  # the lane per stage rather than sending every stage to one queue.
  academic_cpu_lane_enabled = (
    local.academic_execution_enabled &&
    var.reference_data.enabled &&
    var.scheduling.academic_raw_data_stages
  )
  academic_cpu_local_queue_name = format(
    "%s-cpu",
    substr(var.academic_assets.execution.local_queue, 0, 59),
  )
  # Deliberately route-less. A tenant+model+class tuple cannot say whether a
  # stage is CPU or GPU, so duplicating the GPU lane's bindings here would make
  # every AlphaFold 3 route ambiguous. This lane is selected only through the
  # frozen CPU stage class below.
  academic_cpu_local_queues = local.academic_cpu_lane_enabled ? {
    (local.academic_cpu_local_queue_name) = {
      namespace           = var.academic_assets.namespace
      cluster_queue       = var.reference_data.queue.cluster_queue
      fair_sharing_weight = 1
      model_ids           = toset([])
      tenant_ids          = toset([])
      service_classes     = toset([])
    }
  } : {}

  # Named CPU stage classes. Only a class with a real pool, selector,
  # toleration, and advertised per-node capacity is published: an executable
  # class with null placement would let a CPU stage land on arbitrary system or
  # GPU nodes under an accelerator ClusterQueue, so a general CPU class stays
  # absent until a general CPU pool and its own queue are configured.
  scientific_cpu_classes = merge(
    local.academic_cpu_lane_enabled ? {
      reference-data = {
        local_queue   = local.academic_cpu_local_queue_name
        cluster_queue = var.reference_data.queue.cluster_queue
        namespace     = var.academic_assets.namespace
        pool_id       = var.reference_data.storage_contract.cpu_pool.id
        node_selector = var.reference_data.storage_contract.cpu_pool.node_labels
        tolerations = [{
          key      = var.reference_data.storage_contract.cpu_pool.taint.key
          operator = "Equal"
          value    = var.reference_data.storage_contract.cpu_pool.taint.value
          effect   = var.reference_data.storage_contract.cpu_pool.taint.effect
        }]
        # Per NODE, not the pool aggregate: a single stage Pod must fit one
        # node, so a consumer compares its request against this.
        schedulable_capacity = var.reference_data.storage_contract.cpu_pool.schedulable_capacity
      }
    } : {},
  )
  scheduling_required_namespaces = merge(
    local.academic_execution_enabled ? {
      (var.academic_assets.execution.cluster_queue) = [var.academic_assets.namespace]
    } : {},
    local.academic_cpu_lane_enabled ? {
      (var.reference_data.queue.cluster_queue) = [var.academic_assets.namespace]
    } : {},
  )

  queue_default = {
    cluster_queue_name = local.selected_accelerator_pool_profile.queue.cluster_queue_name
    local_queue_name   = local.selected_accelerator_pool_profile.queue.local_queue_name
    namespace          = "fs2-models"
    queueing_strategy  = local.selected_accelerator_pool_profile.queue.queueing_strategy
    additional_namespaces = local.academic_execution_enabled && (
      var.academic_assets.execution.cluster_queue == local.selected_accelerator_pool_profile.queue.cluster_queue_name
    ) ? [var.academic_assets.namespace] : []
    fair_share_precedence_acknowledged = var.scheduling.fair_share_precedence_acknowledged
  }
  queue_common_annotations = {
    "fs2-serve.nebius.ai/accelerator-contract-sha256" = local.accelerator_pool_contract_sha256
  }
}

# The v2 accelerator contract currently records physical GPUs per node but not
# the schedulable extended-resource units advertised per node for MIG slices.
# Refuse to derive Kueue quota for an active MIG pool until that upstream
# contract grows an explicit resource_units_per_node field.
resource "terraform_data" "academic_lane_ownership" {
  input = local.academic_scheduling_local_queues

  lifecycle {
    # A reference-data plane owns a cpu/memory ClusterQueue, and a CPU stage
    # class exists only to run core-requesting work. Either would be a false
    # promise while Kueue drops core requests before admission.
    # A raw academic data stage reads the shared reference databases on a
    # tainted CPU pool and requests core resources, so the mode is only
    # truthful with that plane and core admission both present.
    precondition {
      condition = (
        !var.scheduling.academic_raw_data_stages ||
        (
          local.academic_execution_enabled &&
          var.reference_data.enabled &&
          var.scheduling.core_capacity != null
        )
      )
      error_message = "scheduling.academic_raw_data_stages requires enabled academic execution, an enabled reference-data plane whose CPU pool and ClusterQueue can hold one raw stage Pod, and scheduling.core_capacity so Kueue actually counts the cpu and memory that stage requests. Leave it false to run those models from enriched inputs only."
    }

    precondition {
      condition = (
        var.scheduling.core_capacity != null ||
        (
          !var.reference_data.enabled &&
          length(local.scientific_cpu_classes) == 0 &&
          length(var.scheduling.cpu_stage_requests) == 0
        )
      )
      error_message = "The reference-data plane owns a cpu/memory ClusterQueue and a CPU stage class exists to run core-requesting work, but Kueue drops cpu and memory requests before admission unless core admission is on. Set deployment.scheduling.core_capacity to the exact aggregate schedulable cpu/memory of the pools backing this Kueue installation."
    }

    precondition {
      condition     = length(local.academic_lane_queue_collisions) == 0
      error_message = "A scheduling.local_queues entry collides with the derived academic execution LocalQueue name; rename the operator lane or change academic_assets.execution.local_queue so exactly one definition owns that queue."
    }

  }
}

resource "terraform_data" "queue_accelerator_unit_contract" {
  input = {
    for pool_id, pool in local.selected_queue_pools : pool_id => pool.features.mig.mode
  }

  lifecycle {
    precondition {
      condition = alltrue([
        for pool in values(local.selected_queue_pools) : contains(
          ["disabled", "none"],
          pool.features.mig.mode,
        )
      ])
      error_message = "Kueue quota for active MIG pools is blocked until accelerator-pool v2 reports exact advertised resource_units_per_node; physical GPU count is not a valid MIG slice quota."
    }
  }
}

module "kueue_scheduling" {
  source = "../../modules/kueue-scheduling"

  pools         = local.queue_accelerator_pools
  default_queue = local.queue_default
  # The GPU lane already has an owner in modules/academic-assets, so it is
  # described but not created. The CPU lane is new and has no prior owner or
  # state, so this module creates it.
  scheduling = merge(var.scheduling, {
    local_queues = merge(var.scheduling.local_queues, local.academic_cpu_local_queues)
  })
  external_local_queues  = local.academic_scheduling_local_queues
  namespace_bound_models = local.academic_namespace_bound_models
  cpu_classes            = local.scientific_cpu_classes
  core_capacity          = var.scheduling.core_capacity
  # The reference-data class exists to run the raw AlphaFold 3 data stage, so
  # its request is derived rather than optional: omitting it would bypass every
  # per-node and quota fit check. An operator may raise it, never remove it.
  # Per field max, not merge: an operator may raise the canonical request but
  # cannot lower it, so the node and quota fit checks can never be bypassed.
  cpu_stage_requests = {
    for class_name, request in merge(
      local.academic_cpu_lane_enabled ? { reference-data = local.raw_af3_cpu_request } : {},
      var.scheduling.cpu_stage_requests,
      ) : class_name => class_name != "reference-data" ? request : {
      cpu_millicores = max(request.cpu_millicores, local.raw_af3_cpu_request.cpu_millicores)
      memory_mib     = max(request.memory_mib, local.raw_af3_cpu_request.memory_mib)
    }
  }
  required_namespaces = local.scheduling_required_namespaces
  external_cluster_queues = var.reference_data.enabled ? {
    (var.reference_data.queue.cluster_queue) = {
      namespaces = sort(distinct(compact([
        var.reference_data.namespace,
        local.academic_cpu_lane_enabled ? var.academic_assets.namespace : "",
      ])))
      # The quota its own owner renders, in the units this contract uses.
      core_quota = {
        cpu_millicores = local.reference_queue_cpu_millicores
        memory_mib     = local.reference_queue_memory_mib
      }
    }
  } : {}
  base_priority_classes = var.model_controller.priority_classes
  labels                = local.common_labels
  annotations           = local.queue_common_annotations
}

locals {
  scheduling_contract_key    = "kueue-scheduling.json"
  scheduling_contract_json   = jsonencode(module.kueue_scheduling.contract)
  scheduling_contract_sha256 = module.kueue_scheduling.contract_sha256
  # The content-addressed name makes a policy change restart any consumer that
  # mounts this ConfigMap by name. It also prevents a running controller from
  # observing a partly updated policy while creating an immutable admission.
  scheduling_contract_config_map_name = "fs2-${var.run_id}-scientific-scheduling-${substr(local.scheduling_contract_sha256, 0, 12)}"
}

resource "kubernetes_config_map_v1" "scientific_scheduling_contract" {
  metadata {
    name      = local.scheduling_contract_config_map_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-scheduling-policy"
    })
    annotations = merge(local.queue_common_annotations, {
      "fs2-serve.nebius.ai/scheduling-contract-schema" = module.kueue_scheduling.contract.schema
      "fs2-serve.nebius.ai/scheduling-contract-sha256" = local.scheduling_contract_sha256
    })
  }

  immutable = true
  data = {
    (local.scheduling_contract_key) = local.scheduling_contract_json
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [terraform_data.cluster_contract]
}

moved {
  from = kubernetes_manifest.b300_single_flavor
  to   = kubernetes_manifest.accelerator_flavor["nebius-b300-preemptible-1x"]
}

moved {
  from = kubernetes_manifest.b300_eight_flavor
  to   = kubernetes_manifest.accelerator_flavor["nebius-b300-preemptible-8x"]
}

resource "kubernetes_manifest" "accelerator_flavor" {
  for_each = local.selected_queue_pools

  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name = each.value.scheduling.resource_flavor_name
      labels = merge(local.common_labels, {
        (module.kueue_scheduling.contract.pool_node_label_key) = each.key
      })
      annotations = {
        "fs2-serve.nebius.ai/accelerator-contract-sha256" = local.accelerator_pool_contract_sha256
        "fs2-serve.nebius.ai/capacity-type"               = each.value.capacity.type
        "fs2-serve.nebius.ai/min-nodes"                   = tostring(each.value.capacity.min_nodes)
        "fs2-serve.nebius.ai/max-nodes"                   = tostring(each.value.capacity.max_nodes)
      }
    }
    spec = {
      nodeLabels  = local.resource_flavor_node_labels[each.key]
      tolerations = each.value.scheduling.tolerations
    }
  }

  lifecycle {
    precondition {
      condition = (
        module.kueue_scheduling.contract.pool_node_label_key == "accelerator.fs2.nebius/pool-id" &&
        local.selected_accelerator_pool_profile.enabled &&
        toset(keys(local.selected_accelerator_pool_profile.pools)) == toset(local.accelerator_pool_ids) &&
        each.value.id == each.key &&
        (
          var.accelerator_pool_contract.profile == "custom" ?
          local.selected_accelerator_pool_profile.state == "customer-specified" &&
          each.value.state == "customer-specified" &&
          each.value.evidence.hardware_state == "live-preflight-required" :
          local.selected_accelerator_pool_profile.state == "hardware-validated" &&
          each.value.state == "hardware-validated" &&
          each.value.evidence.hardware_state == "hardware-validated"
        ) &&
        each.value.resource_api.mode == "extended-resource" &&
        each.value.capacity.max_nodes >= each.value.capacity.min_nodes &&
        length(each.value.scheduling.tolerations) <= 8 &&
        length(local.resource_flavor_node_labels[each.key]) <= 8 &&
        alltrue([
          for label, value in local.resource_flavor_node_labels[each.key] :
          each.value.scheduling.stable_node_labels[label] == value
        ]) &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == each.key &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == each.value.accelerator_class
      )
      error_message = "ResourceFlavor ${each.key} is not backed by an enabled, hardware-validated extended-resource pool with at most eight Kueue 0.17.8 tolerations."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

# The single label-less core ResourceFlavor. A core-enabled ClusterQueue stays
# Inactive until it exists, so it is created before any queue.
resource "kubernetes_manifest" "core_flavor" {
  for_each = module.kueue_scheduling.contract.core_resource_flavor == null ? {} : {
    (module.kueue_scheduling.contract.core_resource_flavor.metadata.name) = (
      module.kueue_scheduling.contract.core_resource_flavor
    )
  }

  manifest   = each.value
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "async_cluster_queue" {
  manifest = module.kueue_scheduling.contract.cluster_queues[local.queue_default.cluster_queue_name]
  depends_on = [
    kubernetes_manifest.accelerator_cohort,
    kubernetes_manifest.accelerator_flavor,
    kubernetes_manifest.core_flavor,
  ]
}

resource "kubernetes_manifest" "accelerator_cohort" {
  for_each = module.kueue_scheduling.contract.cohort == null ? {} : {
    (module.kueue_scheduling.contract.cohort.metadata.name) = module.kueue_scheduling.contract.cohort
  }

  manifest = each.value
  depends_on = [
    kubernetes_manifest.accelerator_flavor,
    kubernetes_manifest.core_flavor,
  ]
}

resource "kubernetes_manifest" "additional_cluster_queue" {
  for_each = {
    for queue_name, manifest in module.kueue_scheduling.contract.cluster_queues :
    queue_name => manifest if queue_name != local.queue_default.cluster_queue_name
  }

  manifest = each.value
  depends_on = [
    kubernetes_manifest.accelerator_cohort,
    kubernetes_manifest.accelerator_flavor,
    kubernetes_manifest.core_flavor,
  ]
}

resource "kubernetes_manifest" "model_local_queue" {
  manifest   = module.kueue_scheduling.contract.local_queues[local.queue_default.local_queue_name]
  depends_on = [kubernetes_manifest.async_cluster_queue]
}

# LocalQueue.spec.clusterQueue is immutable in Kueue 0.17.8. Keep an explicit
# binding identity in Terraform state so a changed binding plans replacement
# instead of an in-place API update. Replacement briefly removes that queue;
# operators must drain it first. The stable inference-models queue is guarded
# in the policy module and can never take this path.
resource "terraform_data" "additional_local_queue_binding" {
  for_each = {
    for queue_name, manifest in module.kueue_scheduling.contract.local_queues :
    queue_name => manifest
    if queue_name != local.queue_default.local_queue_name && !contains(
      module.kueue_scheduling.contract.external_local_queue_names, queue_name
    )
  }

  input = {
    namespace     = each.value.metadata.namespace
    cluster_queue = each.value.spec.clusterQueue
  }

  triggers_replace = [
    each.value.metadata.namespace,
    each.value.spec.clusterQueue,
  ]
}

# Exactly one Terraform owner per queue: an external queue is described by the
# contract but created by the owner that also owns its namespace and claim.
resource "kubernetes_manifest" "additional_local_queue" {
  for_each = {
    for queue_name, manifest in module.kueue_scheduling.contract.local_queues :
    queue_name => manifest
    if queue_name != local.queue_default.local_queue_name && !contains(
      module.kueue_scheduling.contract.external_local_queue_names, queue_name
    )
  }

  manifest = each.value

  lifecycle {
    replace_triggered_by = [terraform_data.additional_local_queue_binding[each.key]]
  }

  depends_on = [
    kubernetes_manifest.additional_cluster_queue,
    kubernetes_manifest.async_cluster_queue,
    # A LocalQueue cannot be created before its namespace or its ClusterQueue.
    # The academic namespace belongs to modules/academic-assets and the
    # reference CPU ClusterQueue and its ResourceFlavor belong to
    # modules/reference-data; a count = 0 module reference is still a valid
    # graph edge, so this is safe when either feature is disabled.
    module.academic_assets,
    module.reference_data,
  ]
}

resource "kubernetes_manifest" "model_workload_priority" {
  for_each = module.kueue_scheduling.contract.workload_priority_classes

  manifest = each.value

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-queue-priorities"
  }

  depends_on = [
    kubernetes_manifest.additional_local_queue,
    kubernetes_manifest.model_local_queue,
  ]
}
