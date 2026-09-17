# SAI-22 must not infer third-party logging behavior from first-party source.
# Build the release's complete, exact runtime-image inventory and require one
# independently reviewed marker-negative record for every immutable reference.
# The accepted inventory digest is source-pinned; tfvars alone cannot promote
# an image or manufacture payload-safety evidence.
variable "runtime_log_payload_safety_evidence" {
  description = "Exact-image synthetic-marker negative evidence for every control-plane, model, and enabled scientific runtime image in this release."
  type = object({
    schema        = string
    source_commit = string
    source_tree   = string
    reviewed_at   = string
    images = map(object({
      image_reference                         = string
      image_digest                            = string
      test_definition_sha256                  = string
      sealed_evidence_sha256                  = string
      normal_request_marker_absent            = bool
      normal_response_marker_absent           = bool
      streaming_request_marker_absent         = bool
      streaming_response_marker_absent        = bool
      error_request_marker_absent             = bool
      startup_configuration_marker_absent     = bool
      raw_evidence_contains_customer_payloads = bool
    }))
  })
  default  = null
  nullable = true

  validation {
    condition = var.runtime_log_payload_safety_evidence == null || try(
      var.runtime_log_payload_safety_evidence.schema == "fs2-serve.nebius.ai/runtime-log-payload-safety/v1" &&
      can(regex("^[0-9a-f]{40}$", var.runtime_log_payload_safety_evidence.source_commit)) &&
      can(regex("^[0-9a-f]{40}$", var.runtime_log_payload_safety_evidence.source_tree)) &&
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.runtime_log_payload_safety_evidence.reviewed_at)) &&
      alltrue([
        for key, evidence in var.runtime_log_payload_safety_evidence.images :
        can(regex("^[0-9a-f]{64}$", key)) &&
        can(regex("@sha256:[0-9a-f]{64}$", evidence.image_reference)) &&
        can(regex("^sha256:[0-9a-f]{64}$", evidence.image_digest)) &&
        can(regex("^[0-9a-f]{64}$", evidence.test_definition_sha256)) &&
        can(regex("^[0-9a-f]{64}$", evidence.sealed_evidence_sha256)) &&
        evidence.normal_request_marker_absent &&
        evidence.normal_response_marker_absent &&
        evidence.streaming_request_marker_absent &&
        evidence.streaming_response_marker_absent &&
        evidence.error_request_marker_absent &&
        evidence.startup_configuration_marker_absent &&
        !evidence.raw_evidence_contains_customer_payloads
      ]),
      false,
    )
    error_message = "runtime_log_payload_safety_evidence must contain strict marker-negative, payload-free evidence metadata for digest-qualified images only."
  }
}

locals {
  runtime_log_model_consumers = [
    for model_id in local.selected_model_ids : {
      consumer       = "model/${model_id}"
      image_reference = try(
        var.model_image_overrides[model_id],
        local.catalog_model_runtime_images[model_id],
      )
    }
  ]
  runtime_log_scientific_consumers = var.scientific_batch.enabled ? flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : {
        consumer        = "scientific/${try(model.model_id, "unknown")}/${try(stage.stage_id, "unknown")}"
        image_reference = try(stage.image, "")
      }
    ]
  ]) : []
  runtime_log_consumers = concat(
    [{
      consumer        = "control-plane/fs2-serve-control-plane"
      image_reference = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
    }],
    local.runtime_log_model_consumers,
    local.runtime_log_scientific_consumers,
  )
  runtime_log_image_references = sort(distinct([
    for consumer in local.runtime_log_consumers : consumer.image_reference
  ]))
  runtime_log_images_are_immutable = alltrue([
    for image_reference in local.runtime_log_image_references :
    can(regex("@sha256:[0-9a-f]{64}$", image_reference))
  ])
  expected_runtime_log_image_inventory = {
    for image_reference in local.runtime_log_image_references : sha256(image_reference) => {
      image_reference = image_reference
      image_digest    = try(split("@", image_reference)[1], "unqualified")
      consumers = sort(distinct([
        for consumer in local.runtime_log_consumers : consumer.consumer
        if consumer.image_reference == image_reference
      ]))
    }
  }
  expected_runtime_log_image_inventory_sha256 = sha256(jsonencode(local.expected_runtime_log_image_inventory))

  accepted_runtime_log_payload_safety_inventory_sha256 = null
  runtime_log_payload_safety_record = var.runtime_log_payload_safety_evidence == null ? null : {
    schema = var.runtime_log_payload_safety_evidence.schema
    target = {
      run_id          = var.run_id
      cluster_id      = var.cluster_id
      kube_system_uid = var.kube_system_uid
    }
    source = {
      commit = var.runtime_log_payload_safety_evidence.source_commit
      tree   = var.runtime_log_payload_safety_evidence.source_tree
    }
    reviewed_at               = var.runtime_log_payload_safety_evidence.reviewed_at
    expected_inventory_sha256 = local.expected_runtime_log_image_inventory_sha256
    images = {
      for key, expected in local.expected_runtime_log_image_inventory : key => {
        image_reference = expected.image_reference
        image_digest    = expected.image_digest
        consumers       = expected.consumers
        evidence        = try(var.runtime_log_payload_safety_evidence.images[key], null)
      }
    }
  }
  runtime_log_payload_safety_record_json = (
    local.runtime_log_payload_safety_record == null ? null :
    jsonencode(local.runtime_log_payload_safety_record)
  )
  runtime_log_payload_safety_inventory_sha256 = (
    local.runtime_log_payload_safety_record_json == null ? null :
    sha256(local.runtime_log_payload_safety_record_json)
  )
  runtime_log_payload_safety_ready = (
    local.accepted_runtime_log_payload_safety_inventory_sha256 != null &&
    local.runtime_log_images_are_immutable &&
    local.runtime_log_payload_safety_inventory_sha256 == local.accepted_runtime_log_payload_safety_inventory_sha256 &&
    try(
      toset(keys(var.runtime_log_payload_safety_evidence.images)) == toset(keys(local.expected_runtime_log_image_inventory)) &&
      alltrue([
        for key, expected in local.expected_runtime_log_image_inventory :
        var.runtime_log_payload_safety_evidence.images[key].image_reference == expected.image_reference &&
        var.runtime_log_payload_safety_evidence.images[key].image_digest == expected.image_digest
      ]),
      false,
    )
  )
  runtime_log_payload_safety_config_map_name = (
    local.runtime_log_payload_safety_inventory_sha256 == null ? null :
    "fs2-runtime-log-safety-${substr(local.runtime_log_payload_safety_inventory_sha256, 0, 12)}"
  )
}

resource "kubernetes_config_map_v1" "runtime_log_payload_safety" {
  count = local.runtime_log_payload_safety_ready ? 1 : 0

  metadata {
    name      = local.runtime_log_payload_safety_config_map_name
    namespace = "fs2-system"
    labels    = local.common_labels
  }

  immutable = true
  data = {
    "inventory.json" = local.runtime_log_payload_safety_record_json
  }
}

# Non-secret freshness marker for the cross-state foundation verifier. It is
# rewritten after the control-plane release, datasource, or accepted exact-
# image inventory changes and contains no Helm payload or credential material.
resource "kubernetes_config_map_v1" "loki_client_freshness" {
  metadata {
    name      = "fs2-loki-workloads-freshness"
    namespace = "fs2-system"
    labels    = local.common_labels
  }

  data = {
    "client.json" = jsonencode({
      schema = "fs2-serve.nebius.ai/loki-workloads-freshness/v1"
      target = {
        run_id          = var.run_id
        cluster_id      = var.cluster_id
        kube_system_uid = var.kube_system_uid
      }
      control_plane_helm  = helm_release.control_plane.metadata.revision
      control_plane_image = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
      grafana_datasource = {
        uid              = kubernetes_secret_v1.grafana_datasource.metadata[0].uid
        resource_version = kubernetes_secret_v1.grafana_datasource.metadata[0].resource_version
      }
      payload_safety_inventory = local.runtime_log_payload_safety_ready ? {
        namespace        = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].namespace
        name             = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].name
        uid              = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].uid
        resource_version = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].resource_version
        inventory_sha256 = local.runtime_log_payload_safety_inventory_sha256
        image_count      = length(local.expected_runtime_log_image_inventory)
      } : null
    })
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_secret_v1.grafana_datasource,
    kubernetes_config_map_v1.runtime_log_payload_safety,
  ]
}

output "runtime_log_payload_safety_contract" {
  description = "Machine-enforced exact runtime-image inventory and immutable evidence custody required before Loki auth migration."
  value = {
    schema                    = "fs2-serve.nebius.ai/runtime-log-payload-safety-contract/v1"
    ready                     = local.runtime_log_payload_safety_ready
    expected_image_count      = length(local.expected_runtime_log_image_inventory)
    expected_inventory_sha256 = local.expected_runtime_log_image_inventory_sha256
    accepted_inventory_sha256 = local.accepted_runtime_log_payload_safety_inventory_sha256
    evidence_ref = local.runtime_log_payload_safety_ready ? {
      namespace        = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].namespace
      name             = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].name
      uid              = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].uid
      resource_version = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].resource_version
      inventory_sha256 = local.runtime_log_payload_safety_inventory_sha256
    } : null
  }
}
