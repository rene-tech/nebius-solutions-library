locals {
  modelexpress_managed       = var.model_express.enabled && var.model_express.deployment_mode == "managed"
  modelexpress_nvcr_required = local.modelexpress_managed && startswith(try(var.model_express.server_image.repository, ""), "nvcr.io/")
  modelexpress_model_ids     = sort(keys(var.model_express.models))
  modelexpress_unqualified_model_ids = sort(tolist(setsubtract(
    toset(local.modelexpress_model_ids),
    toset(local.model_controller_dynamic_model_ids),
  )))
  modelexpress_unknown_transport_pool_refs = sort(flatten([
    for model_id, config in var.model_express.models : [
      for pool_id in keys(config.pool_transports) : "${model_id}:${pool_id}"
      if !contains(try(local.model_controller_qualified_pool_ids[model_id], []), pool_id)
    ]
  ]))
  modelexpress_mixed_accelerator_model_ids = sort([
    for model_id in local.modelexpress_model_ids : model_id
    if length(distinct([
      for pool_id in try(local.model_controller_qualified_pool_ids[model_id], []) :
      local.selected_queue_pools[pool_id].accelerator_class
    ])) != 1
  ])
  modelexpress_unsupported_runtime_model_ids = sort([
    for model_id in local.modelexpress_model_ids : model_id
    if try(local.catalog_models[model_id].runtime.kind, null) != "vllm"
  ])
  modelexpress_pull_secret_name = "fs2-modelexpress-nvcrio"
  modelexpress_resource_counts = {
    contract   = var.model_express.enabled ? 1 : 0
    namespace  = local.modelexpress_managed ? 1 : 0
    credential = local.modelexpress_nvcr_required ? 1 : 0
    helm       = local.modelexpress_managed ? 1 : 0
  }
  # Keep the chart input as a first-class value so `terraform test` can plan
  # the actual workloads-stage render without contacting a Kubernetes API.
  modelexpress_helm_values = {
    fullnameOverride = "fs2-modelexpress"
    image = {
      repository = try(var.model_express.server_image.repository, "")
      digest     = try(var.model_express.server_image.digest, "")
      pullPolicy = "IfNotPresent"
    }
    imagePullSecrets = local.modelexpress_nvcr_required ? [{ name = local.modelexpress_pull_secret_name }] : []
    serviceAccount = {
      create    = true
      automount = true
      rbac      = { enabled = true }
    }
    service = {
      type = "ClusterIP"
      port = 8001
    }
    # The managed chart is intentionally single-replica. Recreate avoids a
    # RollingUpdate Multi-Attach deadlock when its cache is an RWO PVC.
    deploymentStrategy = { type = "Recreate" }
    persistence = {
      enabled      = var.model_express.cache.enabled
      storageClass = var.model_express.cache.storage_class == null ? "" : var.model_express.cache.storage_class
      accessMode   = "ReadWriteOnce"
      size         = "${var.model_express.cache.size_gib}Gi"
      mountPath    = "/var/cache/modelexpress"
    }
    env = {
      MODEL_EXPRESS_SERVER_PORT     = "8001"
      MODEL_EXPRESS_LOG_LEVEL       = "info"
      MODEL_EXPRESS_LOG_FORMAT      = "json"
      MODEL_EXPRESS_CACHE_DIRECTORY = "/var/cache/modelexpress"
      MX_METADATA_BACKEND           = "kubernetes"
    }
    podSecurityContext = {
      runAsNonRoot = true
      runAsUser    = 65532
      runAsGroup   = 65532
      fsGroup      = 65532
    }
    securityContext = {
      runAsNonRoot = true
      runAsUser    = 65532
      runAsGroup   = 65532
    }
    extraVolumeMounts = var.model_express.cache.enabled ? [] : [{
      name      = "model-cache-ephemeral"
      mountPath = "/var/cache/modelexpress"
    }]
    extraVolumes = var.model_express.cache.enabled ? [] : [{
      name     = "model-cache-ephemeral"
      emptyDir = {}
    }]
    podLabels = {
      "fs2-serve.nebius.ai/component" = "modelexpress-server"
    }
  }
}

resource "terraform_data" "modelexpress_contract" {
  count = local.modelexpress_resource_counts.contract

  input = {
    deployment_mode = var.model_express.deployment_mode
    endpoint        = var.model_express.endpoint
    model_ids       = local.modelexpress_model_ids
    helm_values     = local.modelexpress_helm_values
  }

  lifecycle {
    precondition {
      condition     = contains(["managed", "external"], var.model_express.deployment_mode)
      error_message = "ModelExpress deployment_mode must be managed or external."
    }

    precondition {
      condition = (
        var.model_express.endpoint != null &&
        can(regex("^[A-Za-z0-9.-]+:[0-9]{1,5}$", var.model_express.endpoint)) &&
        try(tonumber(element(split(":", var.model_express.endpoint), 1)) >= 1, false) &&
        try(tonumber(element(split(":", var.model_express.endpoint), 1)) <= 65535, false)
      )
      error_message = "ModelExpress endpoint must be one explicit gRPC host:port without credentials."
    }

    precondition {
      condition     = length(local.modelexpress_unqualified_model_ids) == 0
      error_message = "Every ModelExpress model must retain an exact controller qualification; ineligible IDs: ${join(", ", local.modelexpress_unqualified_model_ids)}."
    }

    precondition {
      condition     = length(local.modelexpress_unknown_transport_pool_refs) == 0
      error_message = "ModelExpress pool transport overrides must name qualified model pools; invalid model:pool entries: ${join(", ", local.modelexpress_unknown_transport_pool_refs)}."
    }

    precondition {
      condition     = length(local.modelexpress_mixed_accelerator_model_ids) == 0
      error_message = "ModelExpress v0.5.1 permits one exact accelerator class per model binding; mixed-class models: ${join(", ", local.modelexpress_mixed_accelerator_model_ids)}."
    }

    precondition {
      condition     = length(local.modelexpress_unsupported_runtime_model_ids) == 0
      error_message = "ModelExpress v0.5.1 is enabled only for explicitly mapped vLLM catalog runtimes; unsupported models: ${join(", ", local.modelexpress_unsupported_runtime_model_ids)}."
    }
  }
}

resource "kubernetes_namespace_v1" "modelexpress" {
  count = local.modelexpress_resource_counts.namespace

  metadata {
    name   = var.model_express.namespace
    labels = local.common_labels
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.modelexpress_contract]
}

resource "kubernetes_secret_v1" "modelexpress_nvcrio" {
  count = local.modelexpress_resource_counts.credential

  metadata {
    name      = local.modelexpress_pull_secret_name
    namespace = var.model_express.namespace
    labels    = local.common_labels
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = var.nvcrio_dockerconfigjson
  }

  depends_on = [kubernetes_namespace_v1.modelexpress]
}

resource "helm_release" "modelexpress" {
  count = local.modelexpress_resource_counts.helm

  name             = "fs2-modelexpress"
  namespace        = var.model_express.namespace
  chart            = "${path.module}/../../charts/addons/modelexpress"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [yamlencode(local.modelexpress_helm_values)]

  lifecycle {
    precondition {
      condition = (
        var.model_express.server_image != null &&
        can(regex("^sha256:[0-9a-f]{64}$", try(var.model_express.server_image.digest, "")))
      )
      error_message = "Managed ModelExpress requires a digest-pinned server image."
    }

    precondition {
      condition     = !local.modelexpress_nvcr_required || var.nvcrio_dockerconfigjson != null
      error_message = "A managed nvcr.io ModelExpress server requires FS2_NVCR_DOCKERCONFIGJSON."
    }
  }

  depends_on = [
    kubernetes_namespace_v1.modelexpress,
    kubernetes_secret_v1.modelexpress_nvcrio,
  ]
}
