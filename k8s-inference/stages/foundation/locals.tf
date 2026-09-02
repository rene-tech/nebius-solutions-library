locals {
  selected_target        = var.target_contract
  target_contract_sha256 = sha256(jsonencode(var.target_contract))

  capacity_profile_contract              = jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json"))
  legacy_infrastructure_contract_enabled = var.infrastructure_contract != null
  legacy_capacity_profile                = try(var.infrastructure_contract.capacity.profile, null)
  legacy_floor_profile                   = try(var.infrastructure_contract.capacity.floor_profile, null)
  selected_capacity                      = try(local.capacity_profile_contract.capacity_profiles[local.legacy_capacity_profile], null)
  selected_floor                         = try(local.capacity_profile_contract.floor_profiles[local.legacy_floor_profile], null)
  infrastructure_contract_sha256         = local.legacy_infrastructure_contract_enabled ? sha256(jsonencode(var.infrastructure_contract)) : null
  expected_legacy_accelerator_pool_ids   = toset(["nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"])
  expected_infrastructure_contract = !local.legacy_infrastructure_contract_enabled ? null : {
    schema        = "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1"
    source_commit = var.infrastructure_contract.source_commit
    target = {
      project_id = nonsensitive(var.project_id)
      region     = local.selected_target.region
      system_update_strategy = {
        max_surge       = local.selected_target.system_update_strategy.max_surge
        max_unavailable = local.selected_target.system_update_strategy.max_unavailable
      }
    }
    source_registry = {
      id         = local.selected_target.source_registry.id
      project_id = local.selected_target.source_registry.project_id
      fqdn       = local.selected_target.source_registry.fqdn
    }
    capacity = {
      profile               = var.infrastructure_contract.capacity.profile
      floor_profile         = var.infrastructure_contract.capacity.floor_profile
      maximum_gpus          = local.selected_capacity.maximum_gpus
      shared_cache_size_gib = local.selected_capacity.shared_cache_size_gib
      system = {
        capacity        = "regular"
        platform        = "cpu-d3"
        preset          = "8vcpu-32gb"
        nodes           = local.selected_capacity.system_nodes
        max_surge       = local.selected_target.system_update_strategy.max_surge
        max_unavailable = local.selected_target.system_update_strategy.max_unavailable
      }
      gpu_b300_1x = {
        capacity      = "preemptible"
        platform      = "gpu-b300-sxm"
        preset        = "1gpu-24vcpu-346gb"
        gpus_per_node = 1
        min_nodes     = local.selected_floor.gpu_1x_min_nodes
        max_nodes     = local.selected_capacity.gpu_1x_max_nodes
        driver_preset = "cuda13.0"
        local_nvme    = false
      }
      gpu_b300_8x = {
        capacity      = "preemptible"
        platform      = "gpu-b300-sxm"
        preset        = "8gpu-192vcpu-2768gb"
        gpus_per_node = 8
        min_nodes     = local.selected_floor.gpu_8x_min_nodes
        max_nodes     = local.selected_capacity.gpu_8x_max_nodes
        driver_preset = "cuda13.0"
        local_nvme    = true
      }
    }
  }
  legacy_infrastructure_contract_matches_v2 = !local.legacy_infrastructure_contract_enabled || try(
    var.infrastructure_contract.source_commit == var.accelerator_pool_contract.source_commit &&
    var.infrastructure_contract.target.project_id == nonsensitive(var.project_id) &&
    var.infrastructure_contract.target.region == var.accelerator_pool_contract.target_region &&
    var.infrastructure_contract.source_registry.id == var.accelerator_pool_contract.artifact_source.registry.id &&
    var.infrastructure_contract.source_registry.project_id == var.accelerator_pool_contract.artifact_source.registry.project_id &&
    var.infrastructure_contract.source_registry.fqdn == var.accelerator_pool_contract.artifact_source.registry.fqdn &&
    var.infrastructure_contract.capacity.profile == var.accelerator_pool_contract.profile &&
    var.infrastructure_contract.capacity.floor_profile == var.accelerator_pool_contract.floor_profile &&
    var.infrastructure_contract.capacity.maximum_gpus == sum([
      for pool in values(var.accelerator_pool_contract.pools) : pool.node.gpus_per_node * pool.capacity.max_nodes
    ]) &&
    toset(keys(var.accelerator_pool_contract.pools)) == local.expected_legacy_accelerator_pool_ids &&
    length(var.accelerator_pool_contract.capacity_ownership.requested_overrides) == 0 &&
    var.infrastructure_contract.capacity.gpu_b300_1x == {
      capacity      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.type
      platform      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.platform
      preset        = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.preset
      gpus_per_node = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].node.gpus_per_node
      min_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.min_nodes
      max_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.max_nodes
      driver_preset = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.driver.preset
      local_nvme    = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].features.local_cache == "local-nvme"
    } &&
    var.infrastructure_contract.capacity.gpu_b300_8x == {
      capacity      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.type
      platform      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.platform
      preset        = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.preset
      gpus_per_node = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].node.gpus_per_node
      min_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.min_nodes
      max_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.max_nodes
      driver_preset = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.driver.preset
      local_nvme    = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].features.local_cache == "local-nvme"
    },
    false,
  )

  common_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "fs2.nebius.ai/environment"    = "disposable"
    "fs2.nebius.ai/run-id"         = var.run_id
  }

  namespaces = toset([
    "cert-manager",
    "cnpg-system",
    "envoy-gateway-system",
    "fs2-data",
    "fs2-models",
    "fs2-observability",
    "fs2-system",
    "kserve",
    "keda",
    "kueue-system",
  ])

  chart_versions = {
    cert_manager          = "v1.21.1"
    cloudnative_pg        = "0.29.0"
    envoy_gateway         = "v1.8.3"
    filesystem_csi        = "0.1.7"
    keda                  = "2.20.2"
    kueue                 = "0.17.8"
    kserve_crd            = "v0.20.0"
    kserve_resources      = "v0.20.0"
    kube_prometheus_stack = "88.5.4"
    loki                  = "7.3.0"
    opentelemetry         = "0.171.0"
  }

  kubeconfig                  = yamldecode(file(var.kubeconfig_path))
  selected_context            = try(one([for context in local.kubeconfig.contexts : context if context.name == var.kube_context]), null)
  selected_kubeconfig_cluster = try(local.selected_context.context.cluster, null)
  selected_cluster            = try(one([for cluster in local.kubeconfig.clusters : cluster if cluster.name == local.selected_kubeconfig_cluster]), null)
  selected_api_server         = try(local.selected_cluster.cluster.server, null)
  normalized_run_root         = trimsuffix(abspath(var.run_root), "/")
  expected_kubeconfig_path    = "${local.normalized_run_root}/kubeconfig"
}
