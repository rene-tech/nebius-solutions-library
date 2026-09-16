locals {
  existing_scientific_pod_security_labels = {
    "pod-security.kubernetes.io/enforce" = "baseline"
    "pod-security.kubernetes.io/audit"   = "restricted"
    "pod-security.kubernetes.io/warn"    = "restricted"
  }
}

resource "terraform_data" "pod_security_rollout_contract" {
  input = {
    phase                               = var.pod_security_rollout_phase
    host_agent_readiness_receipt_sha256 = var.pod_security_host_agent_readiness_receipt_sha256
    host_agent_restore_receipt_sha256   = var.pod_security_host_agent_restore_receipt_sha256
  }

  lifecycle {
    precondition {
      condition = (
        !contains(["migrate-reference-data", "enforce"], var.pod_security_rollout_phase) ||
        var.pod_security_host_agent_readiness_receipt_sha256 != null
      )
      error_message = "Advance beyond prepare only after all host agents are Ready in the exception namespace and their evidence receipt is supplied."
    }
    precondition {
      condition = (
        var.pod_security_rollout_phase != "rollback-remove-exception" ||
        var.pod_security_host_agent_restore_receipt_sha256 != null
      )
      error_message = "Remove the exception namespace only after restored host agents are Ready and their evidence receipt is supplied."
    }
  }
}

# These namespaces are owned by bounded scientific acceptance/deployment
# workflows outside this state. Manage only the PSA label fields; never adopt
# or replace the Namespace object itself.
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
