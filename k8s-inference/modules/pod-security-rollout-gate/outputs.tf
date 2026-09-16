output "verification" {
  value = local.receipt_required ? terraform_data.verified[0].output : {
    phase          = var.phase
    terminal_state = "unmanaged"
    bundle_sha256  = null
    sequence       = 0
    consumer       = var.consumer_role
  }
}
