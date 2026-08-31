data "nebius_iam_v2_project" "target" {
  id = var.project_id
}

data "nebius_registry_v1_registry" "external" {
  for_each = var.external_registry_ids

  id = each.value
}

data "nebius_vpc_v1_network" "target" {
  parent_id = var.project_id
  name      = local.selected_target.network_name
}

data "nebius_vpc_v1_subnet" "target" {
  parent_id = var.project_id
  name      = local.selected_target.subnet_name
}

locals {
  resource_name = coalesce(var.cluster_name, "${var.name_prefix}-${var.run_id}")

  source_registry_regions = {
    for host in var.registry_delivery.source_hosts :
    host => try(regex("^cr\\.([a-z0-9-]+)\\.nebius\\.cloud(?::[0-9]+)?$", host)[0], null)
  }
  cross_region_source_hosts = sort([
    for host, region in local.source_registry_regions : host
    if region != null && region != local.selected_target.region
  ])

  # Provider 0.6.28 exposes the authoritative CIDRs as one list per private
  # pool. Normalize both the outer and inner collection types before comparing
  # them so a provider tuple cannot fail an otherwise exact CIDR match.
  target_subnet_private_cidrs = toset(flatten([
    for pool in try(data.nebius_vpc_v1_subnet.target.status.ipv4_private_pools, []) :
    try(tolist(pool.cidrs), [])
  ]))

  common_labels = {
    owner       = "k8s-elastic-inference-platform"
    task        = "fs2-terraform-recipe"
    managed-by  = "terraform"
    environment = "fs2-disposable"
    retention   = "ephemeral"
    region      = local.selected_target.region
    run-id      = var.run_id
  }
}

resource "terraform_data" "target_contract" {
  input = {
    project_id          = nonsensitive(data.nebius_iam_v2_project.target.id)
    project_name        = data.nebius_iam_v2_project.target.name
    region              = data.nebius_iam_v2_project.target.region
    network_id          = data.nebius_vpc_v1_network.target.id
    network_name        = data.nebius_vpc_v1_network.target.name
    subnet_id           = data.nebius_vpc_v1_subnet.target.id
    subnet_name         = data.nebius_vpc_v1_subnet.target.name
    private_subnet_cidr = local.selected_target.private_subnet_cidr
    system_update_strategy = {
      max_surge       = local.effective_system_pool.max_surge
      max_unavailable = local.effective_system_pool.max_unavailable
    }
    tenant_id        = data.nebius_iam_v2_project.target.parent_id
    public_edge_mode = var.public_edge_mode
  }

  lifecycle {
    precondition {
      condition = (
        nonsensitive(data.nebius_iam_v2_project.target.id) == nonsensitive(var.project_id) &&
        nonsensitive(data.nebius_iam_v2_project.target.id) == local.selected_target.project_id
      )
      error_message = "The provider resolved a different target project."
    }
    precondition {
      condition = local.selected_target.project_name == null ? true : (
        data.nebius_iam_v2_project.target.name == local.selected_target.project_name
      )
      error_message = "The provider-resolved target project name does not match target_binding or the legacy target catalog."
    }
    precondition {
      condition     = data.nebius_iam_v2_project.target.region == local.selected_target.region
      error_message = "The target project is outside the reviewed region."
    }
    precondition {
      condition     = try(data.nebius_iam_v2_project.target.status.project_state, "") == "ACTIVE"
      error_message = "The target project is not ACTIVE."
    }
    precondition {
      condition = (
        data.nebius_vpc_v1_network.target.name == local.selected_target.network_name &&
        try(data.nebius_vpc_v1_network.target.status.state, "") == "READY"
      )
      error_message = "The selected network is not the exact READY network in the approved target mapping."
    }
    precondition {
      condition = (
        data.nebius_vpc_v1_subnet.target.name == local.selected_target.subnet_name &&
        data.nebius_vpc_v1_subnet.target.network_id == data.nebius_vpc_v1_network.target.id &&
        try(data.nebius_vpc_v1_subnet.target.status.state, "") == "READY" &&
        contains(local.target_subnet_private_cidrs, local.selected_target.private_subnet_cidr)
      )
      error_message = "The selected subnet does not match the READY network or contain the expected private CIDR."
    }
    precondition {
      condition     = local.resource_name == coalesce(var.cluster_name, "fs2-disposable-${var.run_id}")
      error_message = "The owned resource namespace differs from the explicit cluster_name or legacy disposable name."
    }
    precondition {
      condition = (
        local.effective_system_pool.capacity == "regular" &&
        local.effective_system_pool.max_surge + local.effective_system_pool.max_unavailable >= 1
      )
      error_message = "The effective system pool must use regular capacity and retain a nonzero rollout allowance."
    }
    precondition {
      condition = local.using_custom_accelerator_pools ? (
        length(var.accelerator_pool_capacity_overrides) == 0
        ) : (
        length(setsubtract(
          toset(keys(var.accelerator_pool_capacity_overrides)),
          toset(keys(local.selected_accelerator_pool_profile.pools)),
        )) == 0 &&
        alltrue([
          for pool_id, bounds in var.accelerator_pool_capacity_overrides : try(
            bounds.min_nodes <= bounds.max_nodes &&
            bounds.max_nodes <= try(
              local.selected_accelerator_pool_profile.pools[pool_id].max_nodes,
              -1,
            ),
            false,
          )
        ])
      )
      error_message = "Accelerator capacity overrides contain an unknown stable pool ID or exceed the selected profile's reviewed maximum."
    }
    precondition {
      condition = local.using_custom_accelerator_pools ? (
        local.accelerator_profile_supports_floor &&
        toset(keys(local.selected_gpu_pools)) == toset(keys(var.custom_accelerator_pools)) &&
        alltrue([
          for pool_id, pool in local.selected_gpu_pools : (
            pool.id == pool_id &&
            pool.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == pool_id &&
            pool.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == pool.accelerator_class
          )
        ])
        ) : (
        local.selected_accelerator_pool_profile.enabled &&
        local.selected_accelerator_pool_profile.state == "hardware-validated" &&
        local.accelerator_profile_supports_floor &&
        toset(local.selected_accelerator_pool_profile.pool_order) == toset(keys(local.selected_gpu_pools)) &&
        alltrue([
          for pool_id, pool in local.selected_gpu_pools : (
            pool.id == pool_id &&
            pool.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == pool_id &&
            pool.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == pool.accelerator_class
          )
        ]) &&
        length(distinct([
          for pool in values(local.selected_gpu_pools) : pool.scheduling.resource_flavor_name
        ])) == length(local.selected_gpu_pools) &&
        length(distinct([
          for pool in values(local.selected_gpu_pools) : pool.provider.node_group_name_suffix
        ])) == length(local.selected_gpu_pools) &&
        length(distinct([
          for pool in values(local.selected_gpu_pools) : pool.provider.node_group_label
        ])) == length(local.selected_gpu_pools) &&
        length(distinct([
          for pool in values(local.selected_gpu_pools) : jsonencode(pool.scheduling.stable_node_labels)
        ])) == length(local.selected_gpu_pools)
      )
      error_message = "The selected accelerator profile is disabled, lacks the requested floor, has divergent pool membership/identity, or has overlapping node-group, flavor, or scheduling identities."
    }
  }
}
