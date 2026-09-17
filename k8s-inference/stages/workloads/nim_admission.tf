locals {
  nim_admission_required_subjects = toset(flatten([
    for model_id in local.selected_model_ids : [
      "NIMCache/${model_id}",
      "NIMService/${model_id}",
    ] if try(local.catalog_models[model_id].runtime.kind, null) == "nim"
  ]))
  nim_admission_entries = [
    for entry_id in sort(keys(var.nim_operator_admission.entries)) : {
      resource_kind = var.nim_operator_admission.entries[entry_id].resource_kind
      model_id       = var.nim_operator_admission.entries[entry_id].model_id
      security_envelope = {
        subject            = var.nim_operator_admission.entries[entry_id].subject
        subject_sha256     = var.nim_operator_admission.entries[entry_id].subject_sha256
        attestation        = local.verified_runtime_security_authorizations[var.nim_operator_admission.entries[entry_id].authorization_id].attestation
        attestation_sha256 = local.verified_runtime_security_authorizations[var.nim_operator_admission.entries[entry_id].authorization_id].attestation_sha256
      }
    }
  ]
  nim_admission_config = {
    schema              = "fs2-serve.nebius.ai/nim-operator-admission-config/v1"
    namespace           = "fs2-models"
    security_session_id = local.runtime_security_authority_session_id
    trusted_attestors   = local.runtime_security_trusted_attestors
    entries             = local.nim_admission_entries
  }
  nim_admission_config_json = jsonencode(local.nim_admission_config)
  nim_admission_config_name = format(
    "fs2-nim-admission-%s",
    substr(sha256(local.nim_admission_config_json), 0, 16),
  )
  nim_admission_chart_values = {
    enabled         = var.nim_operator_admission.enabled
    replicaCount    = 2
    namespace       = "fs2-models"
    configMapName   = var.nim_operator_admission.enabled ? local.nim_admission_config_name : ""
    configKey       = "admission.json"
    tlsSecretName   = var.nim_operator_admission.tls_secret_name
    caBundle        = var.nim_operator_admission.ca_bundle
    service         = { port = 8443 }
    resources = {
      requests = { cpu = "25m", memory = "64Mi" }
      limits   = { cpu = "250m", memory = "256Mi" }
    }
    nodeSelector = {
      "workload.fs2.nebius/system" = "true"
      "capacity.fs2.nebius/type"   = "regular"
      "capacity.fs2.nebius/pool"   = "system"
    }
    tolerations = []
    affinity    = {}
  }
}

resource "terraform_data" "nim_admission_contract" {
  input = {
    enabled       = var.nim_operator_admission.enabled
    config_sha256 = sha256(local.nim_admission_config_json)
    entries       = [for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"]
  }

  lifecycle {
    precondition {
      condition = !var.nim_operator_admission.enabled || (
        local.runtime_security_authority_consistent &&
        length(local.runtime_security_trusted_attestors) > 0 &&
        toset([
          for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"
        ]) == local.nim_admission_required_subjects &&
        length(local.nim_admission_config_json) <= 900000
      )
      error_message = "NIM admission requires exactly one externally verified NIMCache and NIMService envelope for every selected NIM model under the fixed runtime-security authority."
    }
  }
}

resource "kubernetes_config_map_v1" "nim_admission" {
  count = var.nim_operator_admission.enabled ? 1 : 0

  metadata {
    name      = local.nim_admission_config_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "nim-admission" })
  }
  immutable = true
  data      = { "admission.json" = local.nim_admission_config_json }

  lifecycle { create_before_destroy = true }
  depends_on = [terraform_data.nim_admission_contract]
}
