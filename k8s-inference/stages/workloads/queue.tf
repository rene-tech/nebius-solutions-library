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
      # Kueue budgets the selected extended accelerator resource. Core resource
      # fit remains the Kubernetes scheduler's responsibility, matching the
      # foundation controller configuration.
      capacity = pool.node.gpus_per_node * pool.capacity.max_nodes
    }
  }
  queue_default = {
    cluster_queue_name = local.selected_accelerator_pool_profile.queue.cluster_queue_name
    local_queue_name   = local.selected_accelerator_pool_profile.queue.local_queue_name
    namespace          = "fs2-models"
    queueing_strategy  = local.selected_accelerator_pool_profile.queue.queueing_strategy
  }
  queue_common_annotations = {
    "fs2-serve.nebius.ai/accelerator-contract-sha256" = local.accelerator_pool_contract_sha256
  }
}

module "kueue_scheduling" {
  source = "../../modules/kueue-scheduling"

  pools                 = local.queue_accelerator_pools
  default_queue         = local.queue_default
  scheduling            = var.scheduling
  base_priority_classes = var.model_controller.priority_classes
  labels                = local.common_labels
  annotations           = local.queue_common_annotations
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
        "accelerator.fs2.nebius/pool-id" = each.key
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
        length(local.resource_flavor_node_labels[each.key]) <= 8 &&
        alltrue([
          for label, value in local.resource_flavor_node_labels[each.key] :
          each.value.scheduling.stable_node_labels[label] == value
        ]) &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == each.key &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == each.value.accelerator_class
      )
      error_message = "ResourceFlavor ${each.key} is not backed by an enabled, hardware-validated extended-resource pool."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "async_cluster_queue" {
  manifest = module.kueue_scheduling.contract.cluster_queues[local.queue_default.cluster_queue_name]
  depends_on = [
    kubernetes_manifest.accelerator_cohort,
    kubernetes_manifest.accelerator_flavor,
  ]
}

resource "kubernetes_manifest" "accelerator_cohort" {
  for_each = module.kueue_scheduling.contract.cohort == null ? {} : {
    (module.kueue_scheduling.contract.cohort.metadata.name) = module.kueue_scheduling.contract.cohort
  }

  manifest   = each.value
  depends_on = [kubernetes_manifest.accelerator_flavor]
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
  ]
}

resource "kubernetes_manifest" "model_local_queue" {
  manifest   = module.kueue_scheduling.contract.local_queues[local.queue_default.local_queue_name]
  depends_on = [kubernetes_manifest.async_cluster_queue]
}

resource "kubernetes_manifest" "additional_local_queue" {
  for_each = {
    for queue_name, manifest in module.kueue_scheduling.contract.local_queues :
    queue_name => manifest if queue_name != local.queue_default.local_queue_name
  }

  manifest = each.value
  depends_on = [
    kubernetes_manifest.additional_cluster_queue,
    kubernetes_manifest.async_cluster_queue,
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
