output "contract" {
  description = "Validated non-secret scheduling policy and rendered Kueue manifests."
  value       = local.contract

  precondition {
    condition     = terraform_data.contract.id != null
    error_message = "The scheduling contract validation did not complete."
  }
}
