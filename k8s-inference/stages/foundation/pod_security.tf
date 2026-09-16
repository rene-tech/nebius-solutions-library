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
