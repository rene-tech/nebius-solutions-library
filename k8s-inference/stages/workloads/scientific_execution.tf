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

  academic_execution = try(module.academic_assets.academic_assets.execution, {
    enabled              = false
    namespace            = null
    local_queue          = null
    cluster_queue        = null
    local_queue_manifest = null
  })
  academic_execution_enabled = try(local.academic_execution.enabled, false)
  scientific_queue_namespaces = sort(distinct(compact([
    local.queue_default.namespace,
    local.academic_execution_enabled ? local.academic_execution.namespace : null,
  ])))
  scientific_cluster_queues = {
    for queue_name, queue in module.kueue_scheduling.contract.cluster_queues : queue_name => merge(queue, {
      spec = merge(queue.spec, {
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values = (
              queue_name == local.queue_default.cluster_queue_name ?
              local.scientific_queue_namespaces :
              [queue.spec.namespaceSelector.matchLabels["kubernetes.io/metadata.name"]]
            )
          }]
        }
      })
    })
  }
  scientific_local_queues = merge(
    module.kueue_scheduling.contract.local_queues,
    local.academic_execution_enabled ? {
      (local.academic_execution.local_queue) = local.academic_execution.local_queue_manifest
    } : {},
  )
  scientific_local_queue_routes = merge(
    module.kueue_scheduling.contract.local_queue_routes,
    local.academic_execution_enabled ? {
      (local.academic_execution.local_queue) = {
        namespace     = local.academic_execution.namespace
        cluster_queue = local.academic_execution.cluster_queue
        model_ids     = ["alphafold3"]
        tenant_ids    = []
      }
    } : {},
  )
  scientific_scheduling_contract = merge(module.kueue_scheduling.contract, {
    cluster_queues     = local.scientific_cluster_queues
    local_queues       = local.scientific_local_queues
    local_queue_routes = local.scientific_local_queue_routes
  })
  scientific_scheduling_json   = jsonencode(local.scientific_scheduling_contract)
  scientific_scheduling_digest = sha256(local.scientific_scheduling_json)
  scientific_scheduling_name   = "fs2-scientific-scheduling-${substr(local.scientific_scheduling_digest, 0, 12)}"

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

resource "kubernetes_config_map_v1" "scientific_scheduling" {
  count = var.scientific_batch.enabled ? 1 : 0

  metadata {
    name      = local.scientific_scheduling_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "scientific-scheduling" })
    annotations = {
      "fs2.nebius.ai/kueue-scheduling-sha256" = local.scientific_scheduling_digest
    }
  }

  immutable = true
  data      = { "kueue-scheduling.json" = local.scientific_scheduling_json }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    kubernetes_manifest.additional_local_queue,
    kubernetes_manifest.model_local_queue,
    module.academic_assets,
  ]
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
    scheduling_config_map_name = try(kubernetes_config_map_v1.scientific_scheduling[0].metadata[0].name, null)
    scheduling_sha256          = var.scientific_batch.enabled ? local.scientific_scheduling_digest : null
    cache_claims               = sort(keys(local.enabled_scientific_cache_specs))
    cache_classification       = "auxiliary-compiler-jit-l1-plus-not-l2-snapshot"
  }
}
