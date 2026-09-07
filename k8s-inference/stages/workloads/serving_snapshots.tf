# The same optional checkpoint mechanism serves any qualified model/runtime.
# GPU, driver and kernel compatibility stays in each bundle, not in Terraform.
locals {
  serving_snapshot_bundles = var.model_controller.gpu_snapshots.bundles
  serving_snapshot_enabled = (
    var.model_controller.enabled && length(local.serving_snapshot_bundles) > 0
  )
  serving_snapshot_cache = var.model_controller.gpu_snapshots.cache
  serving_snapshot_source_groups = {
    for id, bundle in local.serving_snapshot_bundles : bundle.source_configmap => {
      for filename, digest in bundle.source_sha256 : filename => file(
        "${local.fs2_root}/models/scientific-snapshot/${filename}"
      )
    }...
  }
  serving_snapshot_entrypoint_groups = {
    for id, bundle in local.serving_snapshot_bundles : bundle.entrypoint_configmap => {
      "serving_entrypoint.py" = file("${local.fs2_root}/models/scientific-snapshot/serving_entrypoint.py")
    }...
  }
  serving_snapshot_network_groups = {
    for id, bundle in local.serving_snapshot_bundles : bundle.network_configmap => {
      iptables = file("${local.fs2_root}/models/scientific-snapshot/iptables")
    }...
  }
  serving_snapshot_address_groups = {
    for id, bundle in local.serving_snapshot_bundles : bundle.address_configmap => {
      "restore_loopback_address.py" = file("${local.fs2_root}/models/scientific-snapshot/restore_loopback_address.py")
    }...
  }
  serving_snapshot_configmaps = merge(
    { for name, sources in local.serving_snapshot_source_groups : name => sources[0] },
    { for name, sources in local.serving_snapshot_entrypoint_groups : name => sources[0] },
    { for name, sources in local.serving_snapshot_network_groups : name => sources[0] },
    { for name, sources in local.serving_snapshot_address_groups : name => sources[0] },
  )
  serving_snapshot_adoption = (
    local.serving_snapshot_enabled && var.model_controller.gpu_snapshots.adopt_existing
  )
}

resource "kubernetes_persistent_volume_claim_v1" "serving_snapshots" {
  count = local.serving_snapshot_enabled && local.serving_snapshot_cache.manage_claim ? 1 : 0
  metadata {
    name      = local.serving_snapshot_cache.claim_name
    namespace = local.inventory.namespace
  }
  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = local.serving_snapshot_cache.storage_class_name
    resources {
      requests = { storage = "${local.serving_snapshot_cache.size_gib}Gi" }
    }
  }
  wait_until_bound = false
}

# Reuse, without importing twice, a claim already owned by scientific_snapshots
# or another Terraform stack. This checks the selected claim really exists.
data "kubernetes_persistent_volume_claim_v1" "serving_snapshot_cache" {
  count = local.serving_snapshot_enabled && !local.serving_snapshot_cache.manage_claim ? 1 : 0
  metadata {
    name      = local.serving_snapshot_cache.claim_name
    namespace = local.inventory.namespace
  }
  depends_on = [kubernetes_persistent_volume_claim_v1.scientific_snapshots]
}

resource "kubernetes_config_map_v1" "serving_snapshot_sources" {
  for_each = local.serving_snapshot_enabled ? local.serving_snapshot_configmaps : {}
  metadata {
    name      = each.key
    namespace = local.inventory.namespace
  }
  data      = each.value
  immutable = true
  lifecycle {
    precondition {
      condition = alltrue([
        for id, bundle in local.serving_snapshot_bundles :
        id == bundle.bundle_id && bundle.pvc == local.serving_snapshot_cache.claim_name &&
        alltrue([
          for filename, digest in bundle.source_sha256 :
          filesha256("${local.fs2_root}/models/scientific-snapshot/${filename}") == digest
        ]) &&
        filesha256("${local.fs2_root}/models/scientific-snapshot/serving_entrypoint.py") == bundle.entrypoint_sha256
      ])
      error_message = "Serving snapshots must reference this cache and the exact source bytes used to qualify their bundle."
    }
  }
}

import {
  for_each = local.serving_snapshot_adoption && local.serving_snapshot_cache.manage_claim ? { cache = true } : {}
  to       = kubernetes_persistent_volume_claim_v1.serving_snapshots[0]
  id       = "${local.inventory.namespace}/${local.serving_snapshot_cache.claim_name}"
}

import {
  for_each = local.serving_snapshot_adoption ? local.serving_snapshot_configmaps : {}
  to       = kubernetes_config_map_v1.serving_snapshot_sources[each.key]
  id       = "${local.inventory.namespace}/${each.key}"
}
