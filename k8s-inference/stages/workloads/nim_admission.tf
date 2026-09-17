locals {
  nim_admission_required_subjects = toset(flatten([
    for model_id in local.selected_model_ids : [
      "NIMCache/${model_id}",
      "NIMService/${model_id}",
    ] if try(local.catalog_models[model_id].runtime.kind, null) == "nim"
  ]))
  nim_admission_required = length(local.nim_admission_required_subjects) > 0
  nim_admission_policy_contract = {
    schema         = "fs2-serve.nebius.ai/nim-admission-policy/v5"
    name           = "fs2-serve-control-plane-nim-admission"
    namespace      = "fs2-models"
    failure_policy = "Fail"
    match_policy   = "Equivalent"
    side_effects   = "None"
    timeout_seconds = 5
    admission_review_versions = ["v1"]
    operations     = ["CREATE", "UPDATE"]
    resources = [
      "apps.nvidia.com/v1alpha1/nimcaches",
      "apps.nvidia.com/v1alpha1/nimservices",
      "apps/v1/deployments",
      "apps/v1/replicasets",
      "apps/v1/statefulsets",
      "batch/v1/jobs",
      "v1/pods",
      "v1/pods/ephemeralcontainers",
    ]
    service = {
      namespace = "fs2-system"
      name      = "fs2-serve-control-plane-nim-admission"
      path      = "/admit"
      port      = 8443
    }
    ca_bundle_sha256 = local.nim_admission_required ? sha256(base64decode(var.nim_operator_admission.ca_bundle)) : ""
    owner_resolution = "live-read-through-exact-uid-chain"
    root_enrollment = {
      namespace   = "fs2-system"
      name_prefix = "fs2-nim-root-"
      storage_kind = "immutable-configmap-create-once"
      reconciler = "persisted-root-readback"
      reconcile_interval_seconds = 2
      admission_behavior = "verify-existing-deny-until-enrolled"
    }
    security_boundary = var.nim_operator_admission.security_boundary
  }
  # Python's shared canonical_bytes() contract terminates canonical JSON with
  # one LF. Bind Terraform to those exact bytes instead of a look-alike hash
  # over jsonencode() without the terminator.
  nim_admission_policy_sha256 = sha256("${jsonencode(local.nim_admission_policy_contract)}\n")
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
    schema              = "fs2-serve.nebius.ai/nim-operator-admission-config/v2"
    namespace           = "fs2-models"
    security_session_id = local.runtime_security_authority_session_id
    trusted_attestors   = local.runtime_security_trusted_attestors
    admission_policy    = local.nim_admission_policy_contract
    admission_policy_sha256 = local.nim_admission_policy_sha256
    entries             = local.nim_admission_entries
  }
  nim_admission_config_json = jsonencode(local.nim_admission_config)
  nim_admission_config_name = format(
    "fs2-nim-admission-%s",
    substr(sha256(local.nim_admission_config_json), 0, 16),
  )
  nim_admission_owner_lookup_namespaces = sort(distinct(concat(
    ["fs2-models"],
    flatten([
      for entry in values(var.nim_operator_admission.entries) : [
        for identity in values(try(entry.subject.actor_identities, {})) : identity.namespace
        if try(identity.kind, "") == "pod-bound-service-account"
      ]
    ]),
  )))
  nim_admission_chart_values = {
    enabled         = true
    required        = local.nim_admission_required
    replicaCount    = 2
    namespace       = "fs2-models"
    configMapName   = local.nim_admission_required ? local.nim_admission_config_name : ""
    configKey       = "admission.json"
    tlsSecretName   = var.nim_operator_admission.tls_secret_name
    caBundle        = var.nim_operator_admission.ca_bundle
    admissionPolicySha256 = local.nim_admission_required ? local.nim_admission_policy_sha256 : ""
    ownerLookupNamespaces = local.nim_admission_required ? local.nim_admission_owner_lookup_namespaces : []
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
    required      = local.nim_admission_required
    config_sha256 = sha256(local.nim_admission_config_json)
    entries       = [for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"]
  }

  lifecycle {
    precondition {
      condition = !local.nim_admission_required || (
        local.runtime_security_authority_consistent &&
        length(local.runtime_security_trusted_attestors) > 0 &&
        can(regex("^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", var.nim_operator_admission.tls_secret_name)) &&
        length(var.nim_operator_admission.ca_bundle) >= 1 &&
        can(base64decode(var.nim_operator_admission.ca_bundle)) &&
        try(var.nim_operator_admission.security_boundary.name, "") == "fs2-platform-security-admission-guard" &&
        can(regex("^[0-9a-f-]{36}$", try(var.nim_operator_admission.security_boundary.policy_uid, ""))) &&
        can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.security_boundary.policy_resource_version, ""))) &&
        can(regex("^[0-9a-f-]{36}$", try(var.nim_operator_admission.security_boundary.binding_uid, ""))) &&
        can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.security_boundary.binding_resource_version, ""))) &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.subject_sha256, ""))) &&
        toset([
          for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"
        ]) == local.nim_admission_required_subjects &&
        alltrue([
          for entry in local.nim_admission_entries :
          try(entry.security_envelope.subject.admission_policy, "") == local.nim_admission_policy_contract.name &&
          try(entry.security_envelope.subject.admission_policy_sha256, "") == local.nim_admission_policy_sha256
        ]) &&
        length(local.nim_admission_config_json) <= 900000
      )
      error_message = "NIM admission requires exactly one externally verified NIMCache and NIMService envelope for every selected NIM model under the fixed runtime-security authority."
    }
  }
}

resource "kubernetes_config_map_v1" "nim_admission" {
  count = local.nim_admission_required ? 1 : 0

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
