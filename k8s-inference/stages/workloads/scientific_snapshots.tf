# Qualified snapshots supplement the unchanged scientific execution recipe.
# The runtime checks the captured image, model, driver and GPU identity and
# falls back to its ordinary loader if restoration is incompatible.
locals {
  scientific_snapshot_bundles = var.scientific_batch.gpu_snapshots.bundles
  scientific_snapshot_enabled = (
    var.scientific_batch.enabled && length(local.scientific_snapshot_bundles) > 0
  )
  # Most captured helpers live together. A model-owned wrapper can retain its
  # canonical source location instead of maintaining a duplicate snapshot copy.
  # Add such exceptions here when qualifying another scientific stage adapter.
  scientific_snapshot_source_paths = {
    "run_esmfold2.py" = "${local.fs2_root}/models/cancer-immunotherapy/images/structure-secondary/run_esmfold2.py"
  }
  scientific_snapshot_cli_paths = merge(local.scientific_snapshot_source_paths, {
    protenix = "${local.fs2_root}/models/scientific-snapshot/protenix_cli_proxy.py"
  })
  scientific_snapshot_sources = {
    for id, bundle in local.scientific_snapshot_bundles : bundle.source_configmap => {
      for filename, digest in bundle.source_sha256 : filename => file(
        lookup(local.scientific_snapshot_source_paths, filename, "${local.fs2_root}/models/scientific-snapshot/${filename}")
      )
    }...
  }
  scientific_snapshot_cli_sources = {
    for id, bundle in local.scientific_snapshot_bundles : bundle.cli_configmap => {
      (bundle.cli_key) = file(lookup(
        local.scientific_snapshot_cli_paths, bundle.cli_key,
        "${local.fs2_root}/models/scientific-snapshot/${bundle.cli_key}"
      ))
    }...
  }
  # Several independently qualified models may share immutable helpers, and
  # one ConfigMap may carry both the server sources and its CLI overlay.
  scientific_snapshot_configmaps = {
    for name in setunion(toset(keys(local.scientific_snapshot_sources)), toset(keys(local.scientific_snapshot_cli_sources))) :
    name => merge(concat(
      try(local.scientific_snapshot_sources[name], []),
      try(local.scientific_snapshot_cli_sources[name], []),
    )...)
  }
  scientific_snapshot_adoption = (
    local.scientific_snapshot_enabled && var.scientific_batch.gpu_snapshots.adopt_existing
  )
}

resource "kubernetes_persistent_volume_claim_v1" "scientific_snapshots" {
  count = local.scientific_snapshot_enabled ? 1 : 0
  metadata {
    name      = var.scientific_batch.gpu_snapshots.cache.claim_name
    namespace = var.scientific_batch.namespace
  }
  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.scientific_batch.gpu_snapshots.cache.storage_class_name
    resources {
      requests = { storage = "${var.scientific_batch.gpu_snapshots.cache.size_gib}Gi" }
    }
  }
  wait_until_bound = false
  lifecycle {
    precondition {
      condition = alltrue([
        for bundle in values(local.scientific_snapshot_bundles) :
        bundle.pvc == var.scientific_batch.gpu_snapshots.cache.claim_name
      ])
      error_message = "Every snapshot bundle must reference the configured shared snapshot cache claim."
    }
  }
}

resource "kubernetes_config_map_v1" "scientific_snapshot_sources" {
  for_each = local.scientific_snapshot_enabled ? local.scientific_snapshot_configmaps : {}
  metadata {
    name      = each.key
    namespace = var.scientific_batch.namespace
  }
  data      = each.value
  immutable = true
  lifecycle {
    precondition {
      condition = alltrue(flatten([
        for bundle in values(local.scientific_snapshot_bundles) : [
          alltrue([
            for filename, digest in bundle.source_sha256 :
            filesha256(lookup(
              local.scientific_snapshot_source_paths, filename,
              "${local.fs2_root}/models/scientific-snapshot/${filename}"
            )) == digest
          ]),
          filesha256(lookup(
            local.scientific_snapshot_cli_paths, bundle.cli_key,
            "${local.fs2_root}/models/scientific-snapshot/${bundle.cli_key}"
          )) == bundle.cli_sha256,
        ]
      ]))
      error_message = "Snapshot source files must match the bytes used to qualify the captured bundle."
    }
  }
}

import {
  for_each = local.scientific_snapshot_adoption ? { cache = true } : {}
  to       = kubernetes_persistent_volume_claim_v1.scientific_snapshots[0]
  id       = "${var.scientific_batch.namespace}/${var.scientific_batch.gpu_snapshots.cache.claim_name}"
}

import {
  for_each = local.scientific_snapshot_adoption ? local.scientific_snapshot_configmaps : {}
  to       = kubernetes_config_map_v1.scientific_snapshot_sources[each.key]
  id       = "${var.scientific_batch.namespace}/${each.key}"
}
