locals {
  existing_scientific_pod_security_labels = {
    "pod-security.kubernetes.io/enforce"         = "baseline"
    "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
    "pod-security.kubernetes.io/audit"           = "restricted"
    "pod-security.kubernetes.io/audit-version"   = var.pod_security_version
    "pod-security.kubernetes.io/warn"            = "restricted"
    "pod-security.kubernetes.io/warn-version"    = var.pod_security_version
  }
  pod_security_receipt_required = var.pod_security_rollout_phase != "prepare"
  reference_data_source_catalog = jsondecode(file("${path.module}/../../reference-data/source-catalog.json"))
  pod_security_receipt_context = {
    cluster_id       = var.cluster_id
    run_id           = var.run_id
    kube_system_uid  = var.kube_system_uid
    deployment_nonce = coalesce(var.pod_security_rollout_receipt.deployment_nonce, "prepare")
    exception_admission_sha256 = sha256(jsonencode({
      policy_sha256 = filesha256("${path.module}/../foundation/pod_security_admission.tf")
      usernames     = sort(tolist(var.pod_security_exception_manager_usernames))
    }))
    psa_version           = var.pod_security_version
    scientific_namespaces = sort(tolist(var.pod_security_existing_scientific_namespaces))
    pvc = {
      namespace     = "fs2-reference-data"
      name          = "fs2-reference-data-rwx"
      uid           = local.pod_security_receipt_required ? data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].uid : "prepare"
      storage_class = "fs2-reference-data-retained-sc"
    }
    dataset = {
      id          = var.reference_data.pipeline.bundle_id
      revision    = try(local.reference_data_source_catalog.bundles[var.reference_data.pipeline.bundle_id].revision, "prepare")
      tree_sha256 = coalesce(var.reference_data.expected_tree_sha256, "")
    }
    storage = {
      filesystem_id   = try(var.reference_data.storage_contract.filesystem.id, "prepare")
      capacity_gib    = try(var.reference_data.storage_contract.filesystem.size_gib, 0)
      claim_size_gib  = 1611
      forbid_deletion = try(var.reference_data.storage_contract.filesystem.forbid_deletion, false)
      retention_mode  = try(var.reference_data.storage_contract.lifecycle.retention_mode, "prepare")
    }
  }
}

data "kubernetes_persistent_volume_claim_v1" "reference_data" {
  count = local.pod_security_receipt_required ? 1 : 0
  metadata {
    name      = "fs2-reference-data-rwx"
    namespace = "fs2-reference-data"
  }
}

module "pod_security_rollout_gate" {
  source = "../../modules/pod-security-rollout-gate"

  phase                     = var.pod_security_rollout_phase
  receipt_bundle_path       = var.pod_security_rollout_receipt.bundle_path
  receipt_public_key_path   = var.pod_security_rollout_receipt.public_key_path
  receipt_public_key_sha256 = var.pod_security_rollout_receipt.public_key_sha256
  expected_context          = local.pod_security_receipt_context
}

resource "terraform_data" "pod_security_rollout_contract" {
  input = module.pod_security_rollout_gate.verification

  lifecycle {
    precondition {
      condition = var.pod_security_rollout_phase == "prepare" || (
        var.reference_data.enabled &&
        var.reference_data.storage_contract.lifecycle.retention_mode == "retain" &&
        var.reference_data.storage_contract.filesystem.forbid_deletion &&
        var.reference_data.storage_contract.filesystem.node_mount_path == "/mnt/fs2-reference-data" &&
        var.reference_data.storage_contract.filesystem.host_path == "/mnt/fs2-reference-data/data" &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611
      )
      error_message = "Every post-prepare phase requires the exact retained reference-data filesystem and signed rollout context."
    }
  }
}

# These namespaces are owned by bounded scientific acceptance/deployment
# workflows outside this state. Manage only the six PSA label fields; never
# adopt or replace the Namespace object itself.
resource "kubernetes_labels" "existing_scientific_pod_security" {
  for_each = var.pod_security_rollout_phase == "enforce" ? var.pod_security_existing_scientific_namespaces : []

  api_version = "v1"
  kind        = "Namespace"

  metadata {
    name = each.value
  }

  labels = local.existing_scientific_pod_security_labels
  force  = false

  depends_on = [terraform_data.pod_security_rollout_contract]
}
