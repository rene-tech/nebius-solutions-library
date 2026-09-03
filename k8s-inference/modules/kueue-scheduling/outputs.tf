output "contract" {
  description = "Validated non-secret scheduling policy and rendered Kueue manifests."
  value       = local.contract

  precondition {
    condition     = terraform_data.contract.id != null
    error_message = "The scheduling contract validation did not complete."
  }
}

output "contract_sha256" {
  description = "Canonical SHA-256 revision of the complete non-secret scheduling contract."
  value       = sha256(jsonencode(local.contract))
}

output "local_queue_namespaces" {
  description = "Namespaces this module creates LocalQueues in, so a caller can order the apply behind each namespace owner. External queues are excluded because their own owner places them."
  value       = sort(distinct([for queue in values(local.managed_local_queues) : queue.namespace]))
}

output "priority_precedence" {
  description = "Per-ClusterQueue admission ordering actually configured in Kueue, for truthful operator explanation."
  value       = local.priority_precedence
}
