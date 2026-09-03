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
      enabled              = var.deployment.storage.reference_data.enabled
      region               = var.deployment.target.region
      namespace            = var.deployment.storage.reference_data.namespace
      cpu_pool_nodes       = var.deployment.storage.reference_data.cpu_pool.node_count
      cpu_pool_preset      = var.deployment.storage.reference_data.cpu_pool.preset
      cpu_pool_schedulable = var.deployment.storage.reference_data.cpu_pool.schedulable_capacity
      retention_mode       = var.deployment.storage.reference_data.lifecycle.retention_mode
      destroy_completion = (
        var.deployment.storage.reference_data.lifecycle.retention_mode == "retain" ?
        "full-stack-destroy-incomplete-infrastructure-retained" :
        "full-only-when-versioned-bucket-empty"
      )
      adoption_required          = var.deployment.storage.reference_data.lifecycle.retention_mode == "retain"
      filesystem_size_gib        = var.deployment.storage.reference_data.filesystem.size_gib
      filesystem_forbid_deletion = var.deployment.storage.reference_data.filesystem.forbid_deletion
      object_storage_max_gib     = var.deployment.storage.reference_data.object_storage.max_size_gib
      object_bucket_name         = local.reference_data_bucket_name
      private_msa_default        = true
      public_msa_opt_in_enabled  = var.deployment.storage.reference_data.network.allow_public_msa_opt_in
      staging_bundle             = var.deployment.storage.reference_data.pipeline.enabled ? var.deployment.storage.reference_data.pipeline.bundle_id : null
      accelerator_pool_mounts = sort([
        for pool_id, pool in var.deployment.accelerator_pools : pool_id
        if pool.reference_data_filesystem
      ])
    }
    scientific_artifacts = {
      enabled        = var.deployment.storage.scientific_artifacts.enabled
      region         = var.deployment.target.region
      bucket_name    = local.scientific_artifacts_bucket_name
      max_size_gib   = var.deployment.storage.scientific_artifacts.object_storage.max_size_gib
      retention_mode = var.deployment.storage.scientific_artifacts.lifecycle.retention_mode
      destroy_completion = (
        var.deployment.storage.scientific_artifacts.lifecycle.retention_mode == "retain" ?
        "full-stack-destroy-incomplete-infrastructure-retained" :
        "full-only-when-versioned-bucket-empty"
      )
      adoption_required       = var.deployment.storage.scientific_artifacts.lifecycle.retention_mode == "retain"
      distinct_from_reference = local.scientific_artifacts_bucket_name != local.reference_data_bucket_name
      artifact_retention_days = var.deployment.storage.scientific_artifacts.retention_days
      handle_ttl_seconds      = var.deployment.storage.scientific_artifacts.handle_ttl_seconds
      max_artifact_bytes      = var.deployment.storage.scientific_artifacts.max_artifact_bytes
      media_types             = sort(tolist(var.deployment.storage.scientific_artifacts.media_types))
      egress_cidrs            = sort(tolist(var.deployment.storage.scientific_artifacts.egress_cidrs))
      secret_delivery         = "MYSTERY_BOX"
      credential_generation   = var.deployment.storage.scientific_artifacts.credential_generation
      credential_secret       = "fs2-system/fs2-serve-artifact-store"
      object_key              = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
    }
    scientific_batch = {
      enabled                 = var.deployment.scientific_batch.enabled
      writes_enabled          = var.deployment.scientific_batch.writes_enabled
      namespace               = var.deployment.scientific_batch.namespace
      artifact_store_required = true
    }
    scheduling = {
      cohort_name         = var.deployment.scheduling.cohort.enabled ? var.deployment.scheduling.cohort.name : null
      cluster_queue_names = sort(keys(var.deployment.scheduling.cluster_queues))
      local_queue_names   = sort(keys(var.deployment.scheduling.local_queues))
      service_classes     = sort(keys(var.deployment.scheduling.service_classes))
    }
    scientific_batch = {
      enabled        = var.deployment.scientific_batch.enabled
      writes_enabled = var.deployment.scientific_batch.writes_enabled
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

output "academic_assets" {
  description = "Portable non-secret academic asset delivery contract: identity, tenant binding, and canonical volume."
  value = {
    enabled        = local.academic_assets_contract.enabled
    project_id     = local.academic_assets_contract.project_id
    region         = local.academic_assets_contract.region
    tenant_id      = local.academic_assets_contract.tenant_id
    institution_id = local.academic_assets_contract.institution_id
    canonical_volume = {
      namespace     = local.academic_assets_contract.namespace
      claim         = local.academic_assets_contract.runtime_claim.name
      storage_gib   = local.academic_assets_contract.runtime_claim.storage_gib
      storage_class = local.academic_assets_contract.runtime_claim.storage_class
      lifecycle     = local.academic_assets_contract.runtime_claim.lifecycle
      mount_root    = local.academic_assets_contract.delivery.mount_root
    }
    retained_quarantine_volume = {
      enabled   = local.academic_assets_contract.legacy_quarantine_claim.enabled
      namespace = local.academic_assets_contract.legacy_quarantine_claim.namespace
      claim     = local.academic_assets_contract.legacy_quarantine_claim.name
      retained  = local.academic_assets_contract.legacy_quarantine_claim.retain
      mountable = false
    }
    embeds_licensed_bytes     = local.academic_assets_contract.delivery.embed_licensed_bytes
    general_shared_cache      = local.academic_assets_contract.delivery.general_shared_cache
    readiness_manifest_sha256 = local.academic_assets_contract.readiness_manifest_sha256
    model_ids                 = sort([for key, asset in local.academic_assets_contract.assets : asset.model_id])
  }
}
