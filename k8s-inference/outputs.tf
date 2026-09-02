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
    reference_data = {
      enabled                   = var.deployment.storage.reference_data.enabled
      region                    = var.deployment.target.region
      filesystem_size_gib       = var.deployment.storage.reference_data.filesystem.size_gib
      object_storage_max_gib    = var.deployment.storage.reference_data.object_storage.max_size_gib
      object_bucket_name        = local.reference_data_bucket_name
      private_msa_default       = true
      public_msa_opt_in_enabled = var.deployment.storage.reference_data.network.allow_public_msa_opt_in
      staging_bundle            = var.deployment.storage.reference_data.pipeline.enabled ? var.deployment.storage.reference_data.pipeline.bundle_id : null
    }
    scheduling = {
      cohort_name         = var.deployment.scheduling.cohort.enabled ? var.deployment.scheduling.cohort.name : null
      cluster_queue_names = sort(keys(var.deployment.scheduling.cluster_queues))
      local_queue_names   = sort(keys(var.deployment.scheduling.local_queues))
      service_classes     = sort(keys(var.deployment.scheduling.service_classes))
    }
    dynamic_models = {
      enabled                                        = var.deployment.dynamic_models.enabled
      writes_enabled                                 = var.deployment.dynamic_models.writes_enabled
      workload_owner                                 = var.deployment.dynamic_models.workload_owner
      bootstrap_model_ids                            = sort(tolist(var.deployment.dynamic_models.bootstrap_model_ids))
      fresh_install                                  = var.deployment.dynamic_models.fresh_install
      handoff_receipt_set                            = var.deployment.dynamic_models.handoff_receipt != null
      fast_start_evidence_file_set                   = var.deployment.dynamic_models.fast_start_evidence_file != null
      fast_start_environment_qualifications_file_set = var.deployment.dynamic_models.fast_start_environment_qualifications_file != null
      fast_start_measurement_contracts_file_set      = var.deployment.dynamic_models.fast_start_measurement_contracts_file != null
      fast_start_wait_second_value                   = var.deployment.dynamic_models.fast_start_wait_second_value
      fast_start_mechanism_hourly_costs              = var.deployment.dynamic_models.fast_start_mechanism_hourly_costs
      priority_classes                               = var.deployment.dynamic_models.priority_classes
    }
    model_express = {
      enabled         = var.deployment.acceleration.model_express.enabled
      deployment_mode = var.deployment.acceleration.model_express.deployment_mode
      endpoint = (
        var.deployment.acceleration.model_express.enabled ?
        local.workloads_variables.model_express.endpoint : null
      )
      metadata_backend                         = var.deployment.acceleration.model_express.metadata_backend
      namespace                                = var.deployment.acceleration.model_express.namespace
      managed_nvcr_server_requires_pull_secret = local.modelexpress_managed_nvcr_server_required
      model_ids                                = sort(keys(var.deployment.acceleration.model_express.models))
      models = {
        for model_id, config in var.deployment.acceleration.model_express.models : model_id => {
          runtime_adapter        = config.runtime_adapter
          client_package_version = config.client_package_version
          transport_default      = config.transport
          pool_transports        = config.pool_transports
        }
      }
    }
    registry_policy = local.deployment_contract.artifact_delivery
  }
}
