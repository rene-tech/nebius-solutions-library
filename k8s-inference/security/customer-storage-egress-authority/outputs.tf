output "current_handoff" {
  description = "Non-secret provider-enforced egress handoff for the additive v2 reconciler."
  value = {
    schema                                        = "fs2-serve.nebius.ai/customer-storage-provider-egress-handoff/v1"
    generation                                    = local.authority.current_generation
    authority_manifest_sha256                     = data.external.authority.result.manifest_sha256
    contract_sha256                               = local.generations[local.authority.current_generation].contract_sha256
    predecessor_compatibility_sha256              = local.generations[local.authority.current_generation].predecessor_compatibility_sha256
    security_group_id                             = nebius_vpc_v1_security_group.generation[local.authority.current_generation].id
    node_group_id                                 = nebius_mk8s_v1_node_group.generation[local.authority.current_generation].id
    node_selector_key                             = "workload.fs2.nebius/customer-storage-egress"
    node_selector_value                           = local.authority.current_generation
    taint_key                                     = "workload.fs2.nebius/customer-storage-egress"
    taint_value                                   = local.authority.current_generation
    taint_effect                                  = "NoSchedule"
    provider_api_cidrs                            = local.generations[local.authority.current_generation].provider_api_cidrs
    kubernetes_api_cidrs                          = local.generations[local.authority.current_generation].kubernetes_api_cidrs
    authority_service_account_sha256              = sha256(data.external.authority.result.authority_service_account_id)
    workloads_service_account_sha256              = sha256(data.external.authority.result.workloads_service_account_id)
    release_service_accounts_sha256               = data.external.authority.result.release_service_account_ids_sha256
    human_principals_sha256                       = data.external.authority.result.human_principal_ids_sha256
    provider_identity_sha256                      = data.external.provider_identity.result.provider_identity_sha256
    kubernetes_security_owner_sha256              = data.external.authority.result.kubernetes_security_owner_sha256
    kubernetes_workloads_sha256                   = data.external.authority.result.kubernetes_workloads_sha256
    kubernetes_non_owner_subjects_sha256          = data.external.authority.result.kubernetes_non_owner_subjects_sha256
    provider_project_iam_inventory_receipt_sha256 = data.external.authority.result.provider_project_iam_inventory_receipt_sha256
  }
}
