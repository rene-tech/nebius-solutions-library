locals {
  approved_target_contract = jsondecode(file("${path.module}/catalog/profiles/approved-targets.json"))
  capacity_contract        = jsondecode(file("${path.module}/catalog/profiles/capacity-profiles.json"))
  accelerator_contract     = jsondecode(file("${path.module}/catalog/profiles/accelerator-pools.json"))
  pool_profile_contract    = jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json"))
  model_profile_contract   = jsondecode(file("${path.module}/catalog/profiles/model-profiles.json"))

  capacity_profile    = var.deployment.profiles.capacity
  accelerator_profile = coalesce(var.deployment.profiles.accelerators, local.capacity_profile)
  model_profile       = var.deployment.profiles.models

  catalog_target                 = try(local.approved_target_contract.targets[var.deployment.target.project_id], null)
  selected_capacity              = local.capacity_contract.capacity_profiles[local.capacity_profile]
  selected_pool_profile          = local.pool_profile_contract.profiles[local.accelerator_profile]
  selected_model_profile         = local.model_profile_contract.profiles[local.model_profile]
  using_custom_accelerator_pools = length(var.deployment.accelerator_pools) > 0

  target_override_requested = anytrue([
    var.deployment.target.project_name != null,
    var.deployment.target.network.network_name != null,
    var.deployment.target.network.subnet_name != null,
    var.deployment.target.network.private_subnet_cidr != null,
    var.deployment.target.system_update_strategy != null,
  ])

  resolved_target_binding = {
    project_id   = var.deployment.target.project_id
    project_name = try(coalesce(var.deployment.target.project_name, try(local.catalog_target.project_name, null)), null)
    region       = var.deployment.target.region
    network_name = try(coalesce(var.deployment.target.network.network_name, try(local.catalog_target.network_name, null)), null)
    subnet_name  = try(coalesce(var.deployment.target.network.subnet_name, try(local.catalog_target.subnet_name, null)), null)
    private_subnet_cidr = try(coalesce(
      var.deployment.target.network.private_subnet_cidr,
      try(local.catalog_target.private_subnet_cidr, null),
    ), null)
    system_update_strategy = try(coalesce(
      var.deployment.target.system_update_strategy,
      try(local.catalog_target.system_update_strategy, null),
    ), null)
  }

  run_id = "r${substr(sha256(jsonencode({
    name       = var.deployment.name
    project_id = var.deployment.target.project_id
    region     = var.deployment.target.region
  })), 0, 10)}"

  selected_model_ids = sort(tolist(
    var.deployment.models.selection == "profile" ?
    toset(local.selected_model_profile.canonical_routes) :
    var.deployment.models.enabled
  ))
  selected_model_required_secrets = toset(distinct(flatten([
    for model_id in local.selected_model_ids : try(
      local.model_profile_contract.model_artifacts[model_id].required_secrets,
      [],
    )
  ])))

  accelerator_pool_capacity_overrides = {
    for pool_id, bounds in var.deployment.accelerator_pool_capacity : pool_id => {
      min_nodes = bounds.min_nodes
      max_nodes = bounds.max_nodes
    }
  }
  effective_pool_capacities = local.using_custom_accelerator_pools ? {
    for pool_id, pool in var.deployment.accelerator_pools : pool_id => {
      min_nodes = pool.min_nodes
      max_nodes = pool.max_nodes
    }
    } : {
    for pool_id, bounds in local.selected_pool_profile.pools : pool_id => {
      min_nodes = try(var.deployment.accelerator_pool_capacity[pool_id].min_nodes, bounds.floor_nodes.zero)
      max_nodes = try(var.deployment.accelerator_pool_capacity[pool_id].max_nodes, bounds.max_nodes)
    }
  }
  effective_pool_facts = local.using_custom_accelerator_pools ? {
    for pool_id, pool in var.deployment.accelerator_pools : pool_id => {
      accelerator_class  = pool.accelerator_class
      gpus_per_node      = pool.gpus_per_node
      host_architectures = [pool.host_architecture]
    }
    } : {
    for pool_id in keys(local.selected_pool_profile.pools) : pool_id => {
      accelerator_class  = local.accelerator_contract.pool_templates[pool_id].accelerator_class
      gpus_per_node      = local.accelerator_contract.pool_templates[pool_id].node.gpus_per_node
      host_architectures = local.accelerator_contract.pool_templates[pool_id].node.host_architectures
    }
  }
  catalog_model_placements = {
    for model_id in local.selected_model_ids : model_id => try(
      local.model_profile_contract.workload_placements[
        local.model_profile_contract.model_autoscaling_targets[model_id].deployment
      ],
      null,
    )
  }
  selected_model_placements = {
    for model_id, placement in local.catalog_model_placements : model_id => (
      contains(keys(var.deployment.models.pool_overrides), model_id) && placement != null ?
      merge(placement, {
        state               = "customer-tfvars"
        selection_mode      = "exact-pool"
        compatible_pool_ids = [var.deployment.models.pool_overrides[model_id]]
        host_architectures  = local.effective_pool_facts[var.deployment.models.pool_overrides[model_id]].host_architectures
        required_node_labels = {
          "accelerator.fs2.nebius/class"   = local.effective_pool_facts[var.deployment.models.pool_overrides[model_id]].accelerator_class
          "accelerator.fs2.nebius/pool-id" = var.deployment.models.pool_overrides[model_id]
        }
      }) : placement
    )
  }
  selected_model_replica_ceilings = {
    for model_id, placement in local.selected_model_placements : model_id => try(floor(
      sum([
        for pool_id in placement.compatible_pool_ids :
        try(local.effective_pool_capacities[pool_id].max_nodes, 0) * (
          try(local.effective_pool_facts[pool_id].gpus_per_node, 0)
        )
      ]) / local.model_profile_contract.model_autoscaling_targets[model_id].gpu_count
    ), 0)
  }

  grafana_external_enabled = var.deployment.observability.grafana.publish_external

  infrastructure_variables = {
    project_id                          = var.deployment.target.project_id
    run_id                              = local.run_id
    cluster_name                        = var.deployment.name
    target_binding                      = local.catalog_target == null || local.target_override_requested ? local.resolved_target_binding : null
    kubernetes_version                  = var.deployment.cluster.kubernetes_version
    control_plane_allowed_cidrs         = sort(tolist(var.deployment.cluster.control_plane_allowed_cidrs))
    capacity_profile                    = local.capacity_profile
    accelerator_pool_profile            = local.accelerator_profile
    gpu_floor_profile                   = "zero"
    accelerator_pool_capacity_overrides = local.accelerator_pool_capacity_overrides
    custom_accelerator_pools            = var.deployment.accelerator_pools
    external_registry_ids               = sort(tolist(var.deployment.artifacts.external_registry_ids))
    system_pool                         = var.deployment.cluster.system_pool
    shared_cache                        = var.deployment.storage.shared_cache
    public_edge_mode                    = var.deployment.edge.mode
    public_edge_source_cidrs            = sort(tolist(var.deployment.edge.source_cidrs))
    port_forward_local_ports            = var.deployment.edge.port_forward_ports
  }

  foundation_variables = {
    grafana_admin_secret_ref = var.deployment.secrets.grafana_admin_secret
    grafana_publication = {
      enabled           = local.grafana_external_enabled
      external_base_url = ""
    }
  }

  workloads_variables = {
    deployment_profile              = local.model_profile
    enabled_model_ids               = local.selected_model_ids
    model_image_overrides           = var.deployment.models.image_overrides
    model_pool_overrides            = var.deployment.models.pool_overrides
    model_scaling_mode              = var.deployment.models.scaling.mode
    hot_model_ids                   = sort(tolist(var.deployment.models.scaling.hot))
    model_scaling_overrides         = var.deployment.models.scaling.overrides
    keda_polling_interval_seconds   = var.deployment.models.scaling.polling_interval_seconds
    keda_cooldown_period_seconds    = var.deployment.models.scaling.cooldown_period_seconds
    keda_fallback_failure_threshold = var.deployment.models.scaling.fallback_failure_threshold
    enable_cold_start_keepers       = var.deployment.models.cold_start_keepers
    enable_dcgm_cold_start_campaign = var.deployment.observability.dcgm_cold_start_campaign
    acme_email                      = var.deployment.edge.acme_email
    run_acceptance_job              = var.deployment.acceptance.create_probe_job
    control_plane_image = {
      repository = var.deployment.applications.control_plane.repository
      digest     = var.deployment.applications.control_plane.digest
    }
    catalog_rollout_digest = var.deployment.applications.control_plane.catalog_rollout_digest
    admin_console = {
      image = {
        repository = var.deployment.applications.admin_console.repository
        digest     = var.deployment.applications.admin_console.digest
      }
      provenance    = var.deployment.applications.admin_console.provenance
      replica_count = var.deployment.applications.admin_console.replica_count
    }
    admin_observability_links = {
      allowed_hosts = []
      grafana = {
        url                     = ""
        verified_external_route = false
      }
      prometheus = { url = "", verified_external_route = false }
      loki       = { url = "", verified_external_route = false }
    }
  }

  deployment_contract_payload = {
    schema_version = 1
    name           = var.deployment.name
    run_id         = local.run_id
    target = {
      project_id = var.deployment.target.project_id
      region     = var.deployment.target.region
    }
    profiles = {
      capacity     = local.capacity_profile
      accelerators = local.accelerator_profile
      models       = local.model_profile
    }
    selected_accelerator_pool_ids   = sort(keys(local.effective_pool_capacities))
    custom_accelerator_pools        = local.using_custom_accelerator_pools
    selected_model_ids              = local.selected_model_ids
    selected_model_replica_ceilings = local.selected_model_replica_ceilings
    stages = {
      infrastructure = local.infrastructure_variables
      foundation     = local.foundation_variables
      workloads      = local.workloads_variables
    }
    secret_environment = {
      grafana_username  = var.deployment.secrets.grafana_username_env
      grafana_password  = var.deployment.secrets.grafana_password_env
      ngc_api_key       = var.deployment.secrets.ngc_api_key_env
      nvcr_dockerconfig = var.deployment.secrets.nvcr_dockerconfig_env
    }
    secret_requirements = {
      grafana_bootstrap = true
      ngc_api_key       = contains(local.selected_model_required_secrets, "ngc_api_key")
      nvcr_dockerconfig = local.model_profile == "full_catalog" || contains(local.selected_model_required_secrets, "nvcr_dockerconfigjson")
    }
  }
  deployment_contract = merge(local.deployment_contract_payload, {
    sha256 = sha256(jsonencode(local.deployment_contract_payload))
  })
}
