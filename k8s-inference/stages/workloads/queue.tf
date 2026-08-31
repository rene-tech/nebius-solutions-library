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
  queue_flavors = [
    for pool_id in local.accelerator_pool_ids : {
      name = local.selected_queue_pools[pool_id].scheduling.resource_flavor_name
      resources = concat(
        local.selected_queue_pools[pool_id].node.vcpu_count == null ? [] : [{
          name         = "cpu"
          nominalQuota = tostring(local.selected_queue_pools[pool_id].node.vcpu_count * local.selected_queue_pools[pool_id].capacity.max_nodes)
        }],
        local.selected_queue_pools[pool_id].node.memory_gib == null ? [] : [{
          name         = "memory"
          nominalQuota = "${local.selected_queue_pools[pool_id].node.memory_gib * local.selected_queue_pools[pool_id].capacity.max_nodes}Gi"
        }],
        [{
          name         = local.selected_queue_pools[pool_id].accelerator.resource_api.resource_name
          nominalQuota = tostring(local.selected_queue_pools[pool_id].node.gpus_per_node * local.selected_queue_pools[pool_id].capacity.max_nodes)
        }],
      )
    }
  ]
  queue_covered_resources = sort(distinct(flatten([
    for flavor in local.queue_flavors : [for resource in flavor.resources : resource.name]
  ])))
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
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata = {
      name   = local.selected_accelerator_pool_profile.queue.cluster_queue_name
      labels = local.common_labels
      annotations = {
        "fs2-serve.nebius.ai/accelerator-contract-sha256" = local.accelerator_pool_contract_sha256
        "fs2-serve.nebius.ai/accelerator-pool-ids"        = join(",", local.accelerator_pool_ids)
      }
    }
    spec = {
      namespaceSelector = {
        matchLabels = { "kubernetes.io/metadata.name" = "fs2-models" }
      }
      queueingStrategy = local.selected_accelerator_pool_profile.queue.queueing_strategy
      resourceGroups = [{
        coveredResources = local.queue_covered_resources
        flavors          = local.queue_flavors
      }]
      stopPolicy = "None"
    }
  }
  depends_on = [kubernetes_manifest.accelerator_flavor]
}

resource "kubernetes_manifest" "model_local_queue" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata = {
      name      = local.selected_accelerator_pool_profile.queue.local_queue_name
      namespace = "fs2-models"
      labels    = local.common_labels
      annotations = {
        "fs2-serve.nebius.ai/accelerator-contract-sha256" = local.accelerator_pool_contract_sha256
      }
    }
    spec = { clusterQueue = local.selected_accelerator_pool_profile.queue.cluster_queue_name }
  }
  depends_on = [kubernetes_manifest.async_cluster_queue]
}
