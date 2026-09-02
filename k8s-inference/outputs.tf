output "deployment_contract" {
  description = "Normalized non-secret contract consumed by the staged orchestrator."
  value       = local.deployment_contract
}

output "effective_configuration" {
  description = "Readable normalized target, profile, pools, and models for review."
  value = {
    name                       = local.deployment_contract.name
    run_id                     = local.deployment_contract.run_id
    target                     = local.deployment_contract.target
    profiles                   = local.deployment_contract.profiles
    contract_sha256            = local.deployment_contract.sha256
    accelerator_pool_ids       = local.deployment_contract.selected_accelerator_pool_ids
    accelerator_pool_overrides = local.accelerator_pool_capacity_overrides
    model_ids                  = local.deployment_contract.selected_model_ids
    edge_mode                  = var.deployment.edge.mode
    port_forward_ports         = var.deployment.edge.port_forward_ports
    model_scaling_mode         = var.deployment.models.scaling.mode
    hot_model_ids              = sort(tolist(var.deployment.models.scaling.hot))
    dynamic_models = {
      enabled             = var.deployment.dynamic_models.enabled
      writes_enabled      = var.deployment.dynamic_models.writes_enabled
      workload_owner      = var.deployment.dynamic_models.workload_owner
      bootstrap_model_ids = sort(tolist(var.deployment.dynamic_models.bootstrap_model_ids))
      fresh_install       = var.deployment.dynamic_models.fresh_install
      handoff_receipt_set = var.deployment.dynamic_models.handoff_receipt != null
      priority_classes    = var.deployment.dynamic_models.priority_classes
    }
    registry_policy = local.deployment_contract.artifact_delivery
  }
}
