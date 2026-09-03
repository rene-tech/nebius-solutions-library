locals {
  scientific_execution_config_map = try(
    jsondecode(var.scientific_batch.execution_map_config_map_json),
    {
      metadata  = { name = "", namespace = "", labels = {}, annotations = {} }
      immutable = false
      data      = { "execution-map.json" = "{}" }
    },
  )
  scientific_execution_map_json = try(
    local.scientific_execution_config_map.data["execution-map.json"],
    "{}",
  )
  scientific_execution_map        = try(jsondecode(local.scientific_execution_map_json), { models = [] })
  scientific_execution_map_digest = sha256(local.scientific_execution_map_json)
  scientific_execution_model_ids = toset([
    for model in try(local.scientific_execution_map.models, []) : model.model_id
  ])

  # Compiler/JIT caches are auxiliary L1+ optimizations, never L2 snapshots.
  scientific_cache_specs = {
    "fs2-academic-poc/scientific-alphafold3-cache" = {
      model_id      = "alphafold3"
      namespace     = "fs2-academic-poc"
      name          = "scientific-alphafold3-cache"
      storage       = "32Gi"
      storage_class = "compute-csi-default-sc"
    }
    "fs2-models/scientific-openfold3-cache" = {
      model_id      = "openfold3"
      namespace     = "fs2-models"
      name          = "scientific-openfold3-cache"
      storage       = "32Gi"
      storage_class = "compute-csi-default-sc"
    }
    "fs2-models/scientific-protenix-cache" = {
      model_id      = "protenix-v2"
      namespace     = "fs2-models"
      name          = "scientific-protenix-cache"
      storage       = "16Gi"
      storage_class = "compute-csi-default-sc"
    }
  }
  enabled_scientific_cache_specs = {
    for key, spec in local.scientific_cache_specs : key => spec
    if var.scientific_batch.enabled && contains(local.scientific_execution_model_ids, spec.model_id)
  }
  execution_map_cache_mounts = toset(flatten([
    for model in try(local.scientific_execution_map.models, []) : [
      for stage in try(model.stages, []) : [
        for mount in stage.mounts : "${mount.claim_namespace}/${mount.claim_name}"
        if mount.kind == "cache"
      ]
    ]
  ]))
}

resource "kubernetes_config_map_v1" "scientific_execution" {
  count = var.scientific_batch.enabled ? 1 : 0

  metadata {
    name        = local.scientific_execution_config_map.metadata.name
    namespace   = "fs2-system"
    labels      = merge(local.common_labels, try(local.scientific_execution_config_map.metadata.labels, {}))
    annotations = local.scientific_execution_config_map.metadata.annotations
  }

  immutable = true
  data      = { "execution-map.json" = local.scientific_execution_map_json }

  lifecycle {
    create_before_destroy = true

    precondition {
      condition = (
        var.scientific_batch.execution_map_config_map_json != null &&
        local.scientific_execution_config_map.metadata.name == "fs2-scientific-execution-${substr(local.scientific_execution_map_digest, 0, 12)}" &&
        local.scientific_execution_config_map.metadata.namespace == "fs2-system" &&
        local.scientific_execution_config_map.immutable == true &&
        local.scientific_execution_config_map.metadata.annotations["fs2.nebius.ai/execution-map-sha256"] == local.scientific_execution_map_digest &&
        local.scientific_execution_map.schema == "fs2-serve.nebius.ai/scientific-execution-map/v3"
      )
      error_message = "scientific_batch.execution_map_file must be the untouched content-addressed ConfigMap emitted by fs2-serve-render-scientific-execution-map."
    }

    precondition {
      condition = (
        !contains(local.scientific_execution_model_ids, "alphafold3") ||
        local.academic_execution_enabled
      )
      error_message = "an AlphaFold 3 execution map requires the Terraform-owned academic namespace, runner, queue and private claim contract."
    }

    precondition {
      condition     = local.execution_map_cache_mounts == toset(keys(local.enabled_scientific_cache_specs))
      error_message = "scientific execution cache mounts must equal the deployment-owned AF3/OpenFold/Protenix L1+ cache PVC allowlist."
    }

    precondition {
      condition = (
        local.scientific_batch_overrides.scientificBatch.executionMapConfigMapName ==
        local.scientific_execution_config_map.metadata.name
      )
      error_message = "the generated scientific execution ConfigMap name must be projected unchanged into the control-plane Helm release."
    }
  }
}

resource "kubernetes_persistent_volume_claim_v1" "scientific_compiler_cache" {
  for_each = local.enabled_scientific_cache_specs

  metadata {
    name      = each.value.name
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-compiler-cache"
      "fs2.nebius.ai/model-id"      = each.value.model_id
      "fs2.nebius.ai/cache-class"   = "auxiliary-compiler-jit-l1-plus"
    })
  }

  wait_until_bound = false
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = each.value.storage_class
    resources {
      requests = { storage = each.value.storage }
    }
  }

  depends_on = [module.academic_assets]
}

resource "terraform_data" "scientific_execution_delivery_contract" {
  input = {
    enabled                    = var.scientific_batch.enabled
    execution_config_map_name  = try(kubernetes_config_map_v1.scientific_execution[0].metadata[0].name, null)
    execution_map_sha256       = var.scientific_batch.enabled ? local.scientific_execution_map_digest : null
    scheduling_config_map_name = var.scientific_batch.enabled ? local.scheduling_contract_config_map_name : null
    scheduling_sha256          = var.scientific_batch.enabled ? local.scheduling_contract_sha256 : null
    cache_claims               = sort(keys(local.enabled_scientific_cache_specs))
    cache_classification       = "auxiliary-compiler-jit-l1-plus-not-l2-snapshot"
  }
}
