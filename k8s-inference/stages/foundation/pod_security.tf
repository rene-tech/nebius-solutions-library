locals {
  pod_security_receipt_required = var.pod_security_rollout_phase != "prepare"
  pod_security_scientific_namespaces = [
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
  ]
  pod_security_retained_context = local.pod_security_receipt_required ? jsondecode(
    data.kubernetes_config_map_v1.reference_data_retained_context[0].data["context.json"]
    ) : {
    pvc = {
      namespace     = "fs2-reference-data"
      name          = "fs2-reference-data-rwx"
      uid           = "prepare"
      storage_class = "fs2-reference-data-retained-sc"
    }
    storage = {
      filesystem_id   = "prepare"
      capacity_gib    = 0
      claim_size_gib  = 0
      forbid_deletion = false
      retention_mode  = "prepare"
    }
  }
  pod_security_receipt_context = {
    cluster_id       = var.cluster_id
    run_id           = var.run_id
    kube_system_uid  = var.kube_system_uid
    deployment_nonce = coalesce(var.pod_security_rollout_receipt.deployment_nonce, "prepare")
    exception_admission_sha256 = sha256(jsonencode({
      policy_sha256 = filesha256("${path.module}/pod_security_admission.tf")
      usernames     = sort(tolist(var.pod_security_exception_manager_usernames))
    }))
    psa_version           = var.pod_security_version
    scientific_namespaces = local.pod_security_scientific_namespaces
    pvc = {
      namespace     = local.pod_security_retained_context.pvc.namespace
      name          = local.pod_security_retained_context.pvc.name
      uid           = local.pod_security_retained_context.pvc.uid
      storage_class = local.pod_security_retained_context.pvc.storage_class
    }
    dataset = var.pod_security_dataset
    storage = local.pod_security_retained_context.storage
  }
}

data "kubernetes_config_map_v1" "reference_data_retained_context" {
  count = local.pod_security_receipt_required ? 1 : 0
  metadata {
    name      = "fs2-reference-data-retained-context"
    namespace = "fs2-system"
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
      condition = !local.pod_security_receipt_required || (
        local.pod_security_retained_context.pvc.uid == data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].uid &&
        local.pod_security_retained_context.pvc.storage_class == data.kubernetes_persistent_volume_claim_v1.reference_data[0].spec[0].storage_class_name &&
        local.pod_security_retained_context.storage.retention_mode == "retain" &&
        local.pod_security_retained_context.storage.forbid_deletion
      )
      error_message = "The signed rollout context must match the live retained PVC UID/class and deletion-protected storage contract."
    }
  }
}
