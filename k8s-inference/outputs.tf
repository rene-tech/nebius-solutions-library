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
      execution_map_source    = local.scientific_execution_map_source
      execution_map_sha256    = local.scientific_execution_map_sha256
    }
    scheduling = {
      cohort_name         = var.deployment.scheduling.cohort.enabled ? var.deployment.scheduling.cohort.name : null
      cluster_queue_names = sort(keys(var.deployment.scheduling.cluster_queues))
      local_queue_names   = sort(keys(var.deployment.scheduling.local_queues))
      service_classes     = sort(keys(var.deployment.scheduling.service_classes))
      # The effective facts a reviewer needs before any stage runs.
      academic_raw_data_stages = var.deployment.scheduling.academic_raw_data_stages
      core_admission           = local.root_core_admission_enabled ? "pool-coupled" : "excluded-not-budgeted"
      core_pool_capacity       = local.root_core_pool_capacity
      cpu_stage_requests       = local.root_cpu_stage_requests
      academic_cpu_local_queue = local.root_academic_cpu_lane_enabled ? local.root_academic_cpu_local_queue_name : null
      reference_cluster_queue  = local.root_academic_cpu_lane_enabled ? local.root_reference_cluster_queue_name : null
      # The order Kueue will try ResourceFlavors in, and the order each
      # service class advertises. They must agree, and neither is meaningful
      # unless a reviewer can read it before anything is created.
      default_queue_pool_order      = local.root_scheduling_queue_pool_order[local.root_default_cluster_queue_name]
      service_class_pool_preference = local.root_service_class_pool_preference
      # Every pool's extended resource, so a mixed-resource eligible set is
      # visible rather than inferred from a pool name.
      # Kueue sums unnormalized quantity magnitudes, so the effective weights
      # and the resources they may name are review facts, not internals.
      budgeted_resource_names     = local.root_budgeted_resource_names
      fair_share_resource_weights = var.deployment.scheduling.fair_share_resource_weights
      pool_resource_names         = local.root_pool_resource_names
      model_eligible_pools        = local.root_model_eligible_pool_ids
    }
    general_cpu = {
      enabled           = local.general_cpu_enabled
      pool_ids          = local.general_cpu_pool_ids
      cluster_queue     = local.general_cpu_enabled ? local.general_cpu_lane.cluster_queue : null
      local_queue       = local.general_cpu_enabled ? local.general_cpu_lane.local_queue : null
      resource_flavor   = local.general_cpu_enabled ? local.general_cpu_lane.resource_flavor : null
      namespace         = local.general_cpu_enabled ? local.general_cpu_namespace : null
      lane_capacity     = local.general_cpu_lane_capacity
      largest_node      = local.general_cpu_largest_node
      elastic_pool_ids  = sort([for pool_id, bounds in local.general_cpu_pool_bounds : pool_id if bounds.elastic])
      scale_from_zero   = sort([for pool_id, bounds in local.general_cpu_pool_bounds : pool_id if bounds.min_nodes == 0])
      preemptible_pools = sort([for pool_id, pool in var.deployment.cpu_pools : pool_id if pool.capacity_type == "preemptible"])
      # The two CPU lanes are separate owners with separate quotas. Neither
      # borrows from nor lends to the other, and neither joins the accelerator
      # cohort.
      cohort = null
      distinct_from_reference_data = (
        !local.general_cpu_enabled ||
        !var.deployment.storage.reference_data.enabled ||
        local.general_cpu_lane.cluster_queue != var.deployment.storage.reference_data.queue.cluster_queue
      )
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
      fast_start_claims                              = var.deployment.storage.fast_start_claims
      priority_classes                               = var.deployment.dynamic_models.priority_classes
    }
    observability = {
      alertmanager = {
        enabled            = var.deployment.observability.alertmanager.enabled
        retention          = var.deployment.observability.alertmanager.retention
        storage_class_name = var.deployment.observability.alertmanager.storage.storage_class_name
        storage_size_gib   = var.deployment.observability.alertmanager.storage.size_gib
      }
      grafana_publish_external = var.deployment.observability.grafana.publish_external
      dcgm_cold_start_campaign = var.deployment.observability.dcgm_cold_start_campaign
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
