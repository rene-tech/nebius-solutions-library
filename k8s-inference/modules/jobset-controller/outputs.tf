output "contract" {
  value = merge(terraform_data.contract.input, {
    ready = var.enabled ? terraform_data.api_ready[0].output : null
  })
}

output "managed_resource_addresses" {
  description = "Closed state-address allowlist for foundation plan review."
  value = var.enabled ? [
    "helm_release.jobset[0]",
    "terraform_data.api_ready[0]",
    "terraform_data.artifacts_verified[0]",
    "terraform_data.crd_upgrade[0]",
    "terraform_data.contract",
    ] : [
    "terraform_data.contract",
  ]
}

output "managed_resource_count" {
  value = var.enabled ? 5 : 1
}
