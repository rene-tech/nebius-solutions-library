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
  selected_runtime_model_contracts = {
    for model_id in local.selected_model_ids :
    model_id => jsondecode(file("${path.module}/catalog/runtime/models/${model_id}.json"))
  }
  effective_model_images = {
    for model_id, model in local.selected_runtime_model_contracts : model_id => try(
      var.deployment.models.image_overrides[model_id],
      model.runtime.image.reference,
    )
  }
  selected_model_required_secrets = toset(distinct(flatten([
    for model_id in local.selected_model_ids : try(
      local.model_profile_contract.model_artifacts[model_id].required_secrets,
      [],
    )
  ])))
  selected_image_source_hosts = sort(distinct(concat(
    [
      split("/", var.deployment.applications.control_plane.repository)[0],
      split("/", var.deployment.applications.admin_console.repository)[0],
    ],
    [for image in values(local.effective_model_images) : split("/", image)[0]],
  )))

  # Nebius Managed Kubernetes builds the cluster-autoscaler template for a
  # zero-node pool from its network boot disk. Host-local NVMe is visible only
  # after a real node joins. The catalog's scheduling request is checked
  # against every selected Deployment manifest by the deployment-contract test.
  selected_model_effective_ephemeral_request_gib = {
    for model_id in local.selected_model_ids :
    model_id => local.model_profile_contract.model_autoscaling_targets[model_id].ephemeral_storage_request_gib
  }

  managed_autoscaler_boot_disk_allocatable_ratio = 0.80
  managed_autoscaler_ephemeral_headroom_gib      = 32

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
      boot_disk_gib      = pool.boot_disk.size_gib
      scale_from_zero    = pool.min_nodes == 0 && pool.topology.mode != "nvlink_rack"
    }
    } : {
    for pool_id in keys(local.selected_pool_profile.pools) : pool_id => {
      accelerator_class  = local.accelerator_contract.pool_templates[pool_id].accelerator_class
      gpus_per_node      = local.accelerator_contract.pool_templates[pool_id].node.gpus_per_node
      host_architectures = local.accelerator_contract.pool_templates[pool_id].node.host_architectures
      boot_disk_gib      = try(local.accelerator_contract.pool_templates[pool_id].node.boot_disk.size_gib, 0)
      scale_from_zero = (
        local.effective_pool_capacities[pool_id].min_nodes == 0 &&
        local.accelerator_contract.pool_templates[pool_id].capacity.scale_from_zero &&
        local.accelerator_contract.pool_templates[pool_id].node.topology != "nvlink_rack"
      )
    }
  }
  effective_pool_synthetic_ephemeral_budget_gib = {
    for pool_id, pool in local.effective_pool_facts : pool_id => max(
      0,
      floor(pool.boot_disk_gib * local.managed_autoscaler_boot_disk_allocatable_ratio) - local.managed_autoscaler_ephemeral_headroom_gib,
    )
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
  scale_from_zero_ephemeral_storage_violations = flatten([
    for model_id, placement in local.selected_model_placements : [
      for pool_id in placement.compatible_pool_ids : format(
        "%s requires %.3f GiB ephemeral storage but pool %s exposes only %.0f GiB in the conservative autoscaler template budget",
        model_id,
        local.selected_model_effective_ephemeral_request_gib[model_id],
        pool_id,
        local.effective_pool_synthetic_ephemeral_budget_gib[pool_id],
        ) if contains(keys(local.effective_pool_facts), pool_id) ? (
        local.effective_pool_facts[pool_id].scale_from_zero &&
        local.selected_model_effective_ephemeral_request_gib[model_id] > local.effective_pool_synthetic_ephemeral_budget_gib[pool_id]
      ) : false
    ]
  ])
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
  modelexpress_managed_nvcr_server_required = (
    var.deployment.acceleration.model_express.enabled &&
    var.deployment.acceleration.model_express.deployment_mode == "managed" &&
    startswith(try(var.deployment.acceleration.model_express.server_image.repository, ""), "nvcr.io/")
  )

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
    registry_delivery = {
      mode              = var.deployment.artifacts.registry_policy.mode
      repository_prefix = var.deployment.artifacts.registry_policy.repository_prefix
      source_hosts      = local.selected_image_source_hosts
    }
    system_pool              = var.deployment.cluster.system_pool
    shared_cache             = var.deployment.storage.shared_cache
    public_edge_mode         = var.deployment.edge.mode
    public_edge_source_cidrs = sort(tolist(var.deployment.edge.source_cidrs))
    port_forward_local_ports = var.deployment.edge.port_forward_ports
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
    model_image_overrides           = local.effective_model_images
    model_pool_overrides            = var.deployment.models.pool_overrides
    model_scaling_mode              = var.deployment.models.scaling.mode
    hot_model_ids                   = sort(tolist(var.deployment.models.scaling.hot))
    model_scaling_overrides         = var.deployment.models.scaling.overrides
    keda_polling_interval_seconds   = var.deployment.models.scaling.polling_interval_seconds
    keda_cooldown_period_seconds    = var.deployment.models.scaling.cooldown_period_seconds
    enable_cold_start_keepers       = var.deployment.models.cold_start_keepers
    enable_dcgm_cold_start_campaign = var.deployment.observability.dcgm_cold_start_campaign
    model_express = {
      enabled         = var.deployment.acceleration.model_express.enabled
      deployment_mode = var.deployment.acceleration.model_express.deployment_mode
      endpoint = (
        var.deployment.acceleration.model_express.deployment_mode == "managed" ?
        "fs2-modelexpress.${var.deployment.acceleration.model_express.namespace}.svc.cluster.local:8001" :
        var.deployment.acceleration.model_express.endpoint
      )
      metadata_backend = var.deployment.acceleration.model_express.metadata_backend
      namespace        = var.deployment.acceleration.model_express.namespace
      server_image     = var.deployment.acceleration.model_express.server_image
      cache            = var.deployment.acceleration.model_express.cache
      external_network = var.deployment.acceleration.model_express.external_network
      models           = var.deployment.acceleration.model_express.models
    }
    acme_email         = var.deployment.edge.acme_email
    acme_environment   = var.deployment.edge.acme_environment
    run_acceptance_job = var.deployment.acceptance.create_probe_job
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
      prometheus   = { url = "", verified_external_route = false }
      loki         = { url = "", verified_external_route = false }
      otel         = { url = "", verified_external_route = false }
      dcgm         = { url = "", verified_external_route = false }
      kueue        = { url = "", verified_external_route = false }
      keda         = { url = "", verified_external_route = false }
      alertmanager = { url = "", verified_external_route = false }
      tempo        = { url = "", verified_external_route = false }
    }
    model_controller = {
      enabled                           = var.deployment.dynamic_models.enabled
      writes_enabled                    = var.deployment.dynamic_models.writes_enabled
      workload_owner                    = var.deployment.dynamic_models.workload_owner
      bootstrap_model_ids               = sort(tolist(var.deployment.dynamic_models.bootstrap_model_ids))
      fresh_install                     = var.deployment.dynamic_models.fresh_install
      handoff_receipt                   = var.deployment.dynamic_models.handoff_receipt
      fast_start_evidence_file          = var.deployment.dynamic_models.fast_start_evidence_file
      fast_start_wait_second_value      = var.deployment.dynamic_models.fast_start_wait_second_value
      fast_start_mechanism_hourly_costs = var.deployment.dynamic_models.fast_start_mechanism_hourly_costs
      priority_classes                  = var.deployment.dynamic_models.priority_classes
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
    selected_model_placements       = local.selected_model_placements
    selected_model_replica_ceilings = local.selected_model_replica_ceilings
    admin_configuration = {
      enabled = true
      source  = "derived-terraform-baseline"
    }
    artifact_delivery = {
      mode                  = var.deployment.artifacts.registry_policy.mode
      repository_prefix     = var.deployment.artifacts.registry_policy.repository_prefix
      upstream_registry_ids = sort(tolist(var.deployment.artifacts.external_registry_ids))
      source_hosts          = local.selected_image_source_hosts
    }
    scale_from_zero_storage = {
      boot_disk_allocatable_ratio       = local.managed_autoscaler_boot_disk_allocatable_ratio
      fixed_headroom_gib                = local.managed_autoscaler_ephemeral_headroom_gib
      model_effective_request_gib       = local.selected_model_effective_ephemeral_request_gib
      pool_synthetic_storage_budget_gib = local.effective_pool_synthetic_ephemeral_budget_gib
    }
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
      nvcr_dockerconfig = (
        local.model_profile == "full_catalog" ||
        contains(local.selected_model_required_secrets, "nvcr_dockerconfigjson") ||
        local.modelexpress_managed_nvcr_server_required
      )
    }
  }
  deployment_contract = merge(local.deployment_contract_payload, {
    sha256 = sha256(jsonencode(local.deployment_contract_payload))
  })
}
