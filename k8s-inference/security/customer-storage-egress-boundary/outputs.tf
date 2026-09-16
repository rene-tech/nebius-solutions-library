output "current_handoff" {
  description = "Value-free, exact handoff consumed by the ordinary workloads root."
  value = {
    schema                        = "fs2-serve.nebius.ai/customer-storage-egress-security-handoff/v1"
    generation                    = var.current_generation
    contract_sha256               = data.external.current_contract.result.contract_sha256
    contract_config_map_name      = local.contract_names[var.current_generation]
    trust_config_map_name         = local.trust_names[local.current_contract.trust_generation]
    network_policy_name           = local.network_policy_names[var.current_generation]
    boundary_policy_name          = local.boundary_policy_names[var.current_boundary_generation]
    security_owner_group          = var.security_owner_group
    security_owner_subject_sha256 = data.external.identity_separation.result.security_owner_subject_sha256
    workloads_subject_sha256      = data.external.identity_separation.result.workloads_subject_sha256
  }
}
