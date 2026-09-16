locals {
  receipt_required = var.phase != "prepare"
}

data "external" "receipt" {
  count   = local.receipt_required ? 1 : 0
  program = ["python3", "${path.module}/../../scripts/verify_pod_security_receipts.py"]

  query = {
    receipt_path      = coalesce(var.receipt_bundle_path, "")
    public_key_path   = coalesce(var.receipt_public_key_path, "")
    public_key_sha256 = coalesce(var.receipt_public_key_sha256, "")
    expected_context  = jsonencode(var.expected_context)
    expected_phase    = var.phase
  }
}

resource "terraform_data" "verified" {
  input = local.receipt_required ? {
    phase            = var.phase
    terminal_state   = data.external.receipt[0].result.terminal_state
    bundle_sha256    = data.external.receipt[0].result.bundle_sha256
    transition_count = tonumber(data.external.receipt[0].result.transition_count)
    } : {
    phase            = var.phase
    terminal_state   = "unmanaged"
    bundle_sha256    = null
    transition_count = 0
  }

  lifecycle {
    precondition {
      condition = !local.receipt_required || (
        var.receipt_bundle_path != null &&
        var.receipt_public_key_path != null &&
        var.receipt_public_key_sha256 != null &&
        try(data.external.receipt[0].result.valid == "true", false)
      )
      error_message = "This phase requires a canonical signed rollout receipt chain verified by the reviewed public-key digest."
    }
  }
}
