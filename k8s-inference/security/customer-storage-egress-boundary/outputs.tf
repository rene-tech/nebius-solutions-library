output "current_handoff" {
  description = "Value-free, exact handoff consumed by the ordinary workloads root."
  value = {
    schema                              = "fs2-serve.nebius.ai/customer-storage-egress-security-handoff/v10"
    generation                          = var.current_generation
    contract_sha256                     = data.external.current_contract.result.contract_sha256
    contract_config_map_name            = local.successor_contract_names[var.current_generation]
    trust_config_map_name               = local.successor_trust_names[local.current_contract.trust_generation]
    network_policy_name                 = local.successor_network_policy_names[var.current_generation]
    boundary_policy_name                = local.successor_boundary_policy_names[var.current_boundary_generation]
    boundary_policy_sha256              = local.boundary_policy_sha256
    workload_policy_name                = local.successor_workload_policy_names[var.current_workload_policy_generation]
    workload_policy_sha256              = local.workload_policy_sha256
    security_owner_group                = var.security_owner_group
    security_owner_subject_sha256       = data.external.identity_separation.result.security_owner_subject_sha256
    workloads_subject_sha256            = data.external.identity_separation.result.workloads_subject_sha256
    identity_inventory_sha256           = data.external.identity_separation.result.identity_inventory_sha256
    protected_observer_inventory_sha256 = var.provider_authority.protected_observer_inventory_sha256
    protected_observer_live_sha256 = sha256(jsonencode({
      for role, observer in data.kubernetes_resource.protected_observer : role => {
        uid         = observer.object.metadata.uid
        spec_sha256 = sha256(jsonencode(observer.object.spec))
      }
    }))
    provider_authority = var.provider_authority
    release_generation = var.current_release_generation
    release_name       = local.release_names[var.current_release_generation]
    predecessor_compatibility = {
      schema                 = "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1"
      receipt_sha256         = local.predecessor_compatibility_sha256
      deployment_uid         = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
      deployment_spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_deployment.object.spec))
      network_policy_uid     = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
      network_policy_sha256  = sha256(jsonencode(data.kubernetes_resource.predecessor_network_policy.object.spec))
      contract_sha256        = data.kubernetes_resource.predecessor_network_policy.object.metadata.annotations["fs2.nebius.ai/storage-egress-contract-sha256"]
      egress_cidrs = [
        for peer in data.kubernetes_resource.predecessor_network_policy.object.spec.egress[2].to : peer.ipBlock.cidr
      ]
      kubernetes_api_cidrs = [
        for peer in data.kubernetes_resource.predecessor_network_policy.object.spec.egress[3].to : peer.ipBlock.cidr
      ]
      contract_uid         = data.kubernetes_resource.predecessor_contract.object.metadata.uid
      contract_data_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_contract.object.data))
      policy_uid           = data.kubernetes_resource.predecessor_policy.object.metadata.uid
      policy_spec_sha256   = sha256(jsonencode(data.kubernetes_resource.predecessor_policy.object.spec))
      binding_uid          = data.kubernetes_resource.predecessor_binding.object.metadata.uid
      binding_spec_sha256  = sha256(jsonencode(data.kubernetes_resource.predecessor_binding.object.spec))
    }
  }
}
