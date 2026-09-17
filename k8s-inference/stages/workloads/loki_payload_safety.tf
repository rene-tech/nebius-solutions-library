# SAI-22 must not infer third-party logging behavior from first-party source.
# Build the release's complete, exact runtime-image inventory and require one
# independently reviewed marker-negative record for every immutable reference.
# The accepted inventory digest is source-pinned; tfvars alone cannot promote
# an image or manufacture payload-safety evidence.
variable "runtime_log_payload_safety_evidence" {
  description = "Owner-enumerated exact-image synthetic-marker negative evidence for every live Pod/controller, Terraform runtime address, static manifest and catalog binding in the release. This input prepares an admission permit but cannot authorize Loki auth without the separate signed release-owner projection."
  type = object({
    schema        = string
    source_commit = string
    source_tree   = string
    reviewed_at   = string
    coverage = object({
      complete   = bool
      namespaces = set(string)
      live_pods = object({
        count  = number
        sha256 = string
      })
      live_workload_controllers = object({
        count  = number
        sha256 = string
      })
      terraform_runtime_addresses = object({
        count  = number
        sha256 = string
      })
      static_runtime_manifests = object({
        count  = number
        sha256 = string
      })
      catalog_runtime_bindings = object({
        count  = number
        sha256 = string
      })
    })
    images = map(object({
      image_reference                         = string
      image_digest                            = string
      consumers                               = set(string)
      inventory_sources                       = set(string)
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
      var.runtime_log_payload_safety_evidence.schema == "fs2-serve.nebius.ai/runtime-log-payload-safety/v2" &&
      can(regex("^[0-9a-f]{40}$", var.runtime_log_payload_safety_evidence.source_commit)) &&
      can(regex("^[0-9a-f]{40}$", var.runtime_log_payload_safety_evidence.source_tree)) &&
      can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.runtime_log_payload_safety_evidence.reviewed_at)) &&
      var.runtime_log_payload_safety_evidence.coverage.complete &&
      length(var.runtime_log_payload_safety_evidence.coverage.namespaces) >= 1 &&
      alltrue([
        for namespace in var.runtime_log_payload_safety_evidence.coverage.namespaces :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace))
      ]) &&
      alltrue([
        for item in [
          var.runtime_log_payload_safety_evidence.coverage.live_pods,
          var.runtime_log_payload_safety_evidence.coverage.live_workload_controllers,
          var.runtime_log_payload_safety_evidence.coverage.terraform_runtime_addresses,
          var.runtime_log_payload_safety_evidence.coverage.static_runtime_manifests,
          var.runtime_log_payload_safety_evidence.coverage.catalog_runtime_bindings,
        ] :
        floor(item.count) == item.count && item.count >= 1 && can(regex("^[0-9a-f]{64}$", item.sha256))
      ]) &&
      alltrue([
        for key, evidence in var.runtime_log_payload_safety_evidence.images :
        can(regex("^[0-9a-f]{64}$", key)) &&
        can(regex("@sha256:[0-9a-f]{64}$", evidence.image_reference)) &&
        can(regex("^sha256:[0-9a-f]{64}$", evidence.image_digest)) &&
        key == sha256(evidence.image_reference) &&
        evidence.image_digest == try(split("@", evidence.image_reference)[1], "") &&
        length(evidence.consumers) >= 1 &&
        alltrue([for consumer in evidence.consumers : length(trimspace(consumer)) >= 1]) &&
        length(evidence.inventory_sources) >= 1 &&
        length(setsubtract(
          evidence.inventory_sources,
          toset(["live-pod", "live-controller", "terraform-address", "static-manifest", "catalog-binding"]),
        )) == 0 &&
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
    error_message = "runtime_log_payload_safety_evidence must contain complete five-source runtime coverage and strict marker-negative, payload-free evidence metadata for digest-qualified images only."
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
  runtime_log_protected_namespaces = sort(distinct(concat(
    ["fs2-system", "fs2-models"],
    local.runtime_attribution_namespaces,
    local.modelexpress_managed ? [var.model_express.namespace] : [],
  )))
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
    reviewed_at = var.runtime_log_payload_safety_evidence.reviewed_at
    coverage = {
      complete          = var.runtime_log_payload_safety_evidence.coverage.complete
      namespaces        = sort(tolist(var.runtime_log_payload_safety_evidence.coverage.namespaces))
      namespaces_sha256 = sha256(jsonencode(sort(tolist(var.runtime_log_payload_safety_evidence.coverage.namespaces))))
      live_pods                    = var.runtime_log_payload_safety_evidence.coverage.live_pods
      live_workload_controllers    = var.runtime_log_payload_safety_evidence.coverage.live_workload_controllers
      terraform_runtime_addresses = var.runtime_log_payload_safety_evidence.coverage.terraform_runtime_addresses
      static_runtime_manifests     = var.runtime_log_payload_safety_evidence.coverage.static_runtime_manifests
      catalog_runtime_bindings     = var.runtime_log_payload_safety_evidence.coverage.catalog_runtime_bindings
    }
    required_desired_inventory_sha256 = local.expected_runtime_log_image_inventory_sha256
    images = {
      for key, evidence in var.runtime_log_payload_safety_evidence.images : key => {
        image_reference                         = evidence.image_reference
        image_digest                            = evidence.image_digest
        consumers                               = sort(tolist(evidence.consumers))
        inventory_sources                       = sort(tolist(evidence.inventory_sources))
        test_definition_sha256                  = evidence.test_definition_sha256
        sealed_evidence_sha256                  = evidence.sealed_evidence_sha256
        normal_request_marker_absent            = evidence.normal_request_marker_absent
        normal_response_marker_absent           = evidence.normal_response_marker_absent
        streaming_request_marker_absent         = evidence.streaming_request_marker_absent
        streaming_response_marker_absent        = evidence.streaming_response_marker_absent
        error_request_marker_absent             = evidence.error_request_marker_absent
        startup_configuration_marker_absent     = evidence.startup_configuration_marker_absent
        raw_evidence_contains_customer_payloads = evidence.raw_evidence_contains_customer_payloads
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
      var.runtime_log_payload_safety_evidence.coverage.complete &&
      length(setsubtract(
        toset(local.runtime_log_protected_namespaces),
        var.runtime_log_payload_safety_evidence.coverage.namespaces,
      )) == 0 &&
      length(setsubtract(
        toset(keys(local.expected_runtime_log_image_inventory)),
        toset(keys(var.runtime_log_payload_safety_evidence.images)),
      )) == 0 &&
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
  # The admission policy reads only the digest-keyed exact image values;
  # inventory.json remains the signed/custodied audit record.
  data = merge(
    { "inventory.json" = local.runtime_log_payload_safety_record_json },
    {
      for key, evidence in var.runtime_log_payload_safety_evidence.images :
      "image-${key}" => evidence.image_reference
    },
  )
}

# A signed owner projection must enumerate existing Pods/controllers because
# admission cannot retroactively inspect them. This policy covers subsequent
# Pod creates/updates (including ephemeral containers) in every enumerated
# runtime namespace and denies any image absent from the immutable permit.
resource "kubernetes_manifest" "runtime_log_payload_safety_policy" {
  count = local.runtime_log_payload_safety_ready ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-runtime-log-payload-safety"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      paramKind = {
        apiVersion = "v1"
        kind       = "ConfigMap"
      }
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["pods", "pods/ephemeralcontainers"]
        }]
      }
      validations = [{
        expression = "object.spec.containers.all(c, params.data.exists(k, k.startsWith('image-') && params.data[k] == c.image)) && (!has(object.spec.initContainers) || object.spec.initContainers.all(c, params.data.exists(k, k.startsWith('image-') && params.data[k] == c.image))) && (!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.all(c, params.data.exists(k, k.startsWith('image-') && params.data[k] == c.image)))"
        message    = "Every runtime container image must be digest-pinned and present in the accepted payload-safety permit."
        reason     = "Forbidden"
      }]
    }
  }

  depends_on = [kubernetes_config_map_v1.runtime_log_payload_safety]
}

resource "kubernetes_manifest" "runtime_log_payload_safety_binding" {
  count = local.runtime_log_payload_safety_ready ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-runtime-log-payload-safety"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-runtime-log-payload-safety"
      validationActions = ["Deny"]
      paramRef = {
        name                    = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].name
        namespace               = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].namespace
        parameterNotFoundAction = "Deny"
      }
      matchResources = {
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = sort(tolist(var.runtime_log_payload_safety_evidence.coverage.namespaces))
          }]
        }
      }
    }
  }

  depends_on = [kubernetes_manifest.runtime_log_payload_safety_policy]
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
        image_count      = length(var.runtime_log_payload_safety_evidence.images)
      } : null
    })
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_secret_v1.grafana_datasource,
    kubernetes_config_map_v1.runtime_log_payload_safety,
    kubernetes_manifest.runtime_log_payload_safety_policy,
    kubernetes_manifest.runtime_log_payload_safety_binding,
  ]
}

output "runtime_log_payload_safety_contract" {
  description = "Machine-enforced exact runtime-image inventory and immutable evidence custody required before Loki auth migration."
  value = {
    schema                    = "fs2-serve.nebius.ai/runtime-log-payload-safety-contract/v1"
    ready                     = local.runtime_log_payload_safety_ready
    expected_image_count      = try(length(var.runtime_log_payload_safety_evidence.images), 0)
    expected_inventory_sha256 = local.expected_runtime_log_image_inventory_sha256
    accepted_inventory_sha256 = local.accepted_runtime_log_payload_safety_inventory_sha256
    owner_signature_required_for_auth = true
    protected_namespaces = local.runtime_log_payload_safety_ready ? sort(tolist(var.runtime_log_payload_safety_evidence.coverage.namespaces)) : []
    admission = local.runtime_log_payload_safety_ready ? {
      policy   = kubernetes_manifest.runtime_log_payload_safety_policy[0].object.metadata.name
      binding  = kubernetes_manifest.runtime_log_payload_safety_binding[0].object.metadata.name
      fail_open = false
    } : null
    evidence_ref = local.runtime_log_payload_safety_ready ? {
      namespace        = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].namespace
      name             = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].name
      uid              = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].uid
      resource_version = kubernetes_config_map_v1.runtime_log_payload_safety[0].metadata[0].resource_version
      inventory_sha256 = local.runtime_log_payload_safety_inventory_sha256
    } : null
  }
}
