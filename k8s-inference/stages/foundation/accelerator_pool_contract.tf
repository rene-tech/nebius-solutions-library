variable "accelerator_pool_contract" {
  description = "Exact resolved accelerator_pool_contract output from the infrastructure state. This v2 contract is authoritative for accelerator identity, placement, and effective capacity."
  type = object({
    schema        = string
    source_commit = string
    profile       = string
    floor_profile = string
    target_region = string
    capacity_ownership = object({
      owner_root                 = string
      override_mode              = string
      override_fields            = list(string)
      requested_overrides        = map(object({ min_nodes = number, max_nodes = number }))
      requested_overrides_sha256 = string
    })
    artifact_source = object({
      registry = object({
        id           = string
        project_id   = string
        project_name = string
        region       = string
        fqdn         = string
      })
      closure_schema             = string
      closure_sha256             = string
      cross_region_pull_required = bool
    })
    pools = map(object({
      id                = string
      accelerator_class = string
      resource_api = object({
        mode          = string
        resource_name = string
      })
      provider = object({
        name                   = string
        platform               = string
        preset                 = string
        node_group_name_suffix = string
        node_group_label       = string
        os                     = string
        driver = object({
          owner  = string
          preset = string
        })
        reservation_policy = string
      })
      node = object({
        gpus_per_node         = number
        gpu_memory_gb_nominal = number
        vcpu_count            = number
        memory_gib            = number
        host_architectures    = list(string)
        topology              = string
        boot_disk = object({
          size_gib = number
          type     = string
        })
        drain_timeout = string
      })
      capacity = object({
        type            = string
        min_nodes       = number
        max_nodes       = number
        source          = string
        profile_bounds  = object({ min_nodes = number, max_nodes = number })
        scale_from_zero = bool
      })
      scheduling = object({
        stable_node_labels   = map(string)
        resource_flavor_name = string
        taints = list(object({
          key    = string
          value  = string
          effect = string
        }))
        tolerations = list(object({
          key      = string
          operator = string
          value    = string
          effect   = string
        }))
        forbidden_scale_zero_selectors = list(string)
      })
      features = object({
        mig = object({
          mode              = string
          resource_strategy = string
        })
        local_storage = object({
          mode            = string
          provider_config = string
        })
        shared_filesystem = bool
        local_cache       = string
        gpu_snapshot      = string
      })
      region_availability = list(object({
        region         = string
        state          = string
        capacity_modes = list(string)
      }))
      state = string
      evidence = object({
        hardware_state = string
        reference      = string
      })
    }))
  })
  nullable = false

  validation {
    condition = try(
      var.accelerator_pool_contract.schema == "fs2-serve.nebius.ai/terraform-accelerator-pools/v2" &&
      can(regex("^[0-9a-f]{40}$", var.accelerator_pool_contract.source_commit)) &&
      can(regex("^[a-z0-9][a-z0-9_-]{0,63}$", var.accelerator_pool_contract.profile)) &&
      can(regex("^[a-z0-9][a-z0-9_-]{0,63}$", var.accelerator_pool_contract.floor_profile)) &&
      length(trimspace(var.accelerator_pool_contract.target_region)) > 0 &&
      length(var.accelerator_pool_contract.pools) >= 1 &&
      length(var.accelerator_pool_contract.pools) <= 128 &&
      (var.accelerator_pool_contract.profile == "custom" || contains(
        keys(jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles),
        var.accelerator_pool_contract.profile,
      )) &&
      (var.accelerator_pool_contract.profile == "custom" || (
        jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles[var.accelerator_pool_contract.profile].enabled &&
        jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles[var.accelerator_pool_contract.profile].state == "hardware-validated"
      )),
      false,
    )
    error_message = "accelerator_pool_contract must be a nonempty v2 contract with an exact source commit and bounded profile, floor, region, and pool identities."
  }

  validation {
    condition = try(
      var.accelerator_pool_contract.capacity_ownership.owner_root == "infra-disposable" &&
      var.accelerator_pool_contract.capacity_ownership.override_mode == "capacity-only-patch" &&
      toset(var.accelerator_pool_contract.capacity_ownership.override_fields) == toset(["min_nodes", "max_nodes"]) &&
      var.accelerator_pool_contract.capacity_ownership.requested_overrides_sha256 == sha256(jsonencode(var.accelerator_pool_contract.capacity_ownership.requested_overrides)) &&
      length(setsubtract(
        toset(keys(var.accelerator_pool_contract.capacity_ownership.requested_overrides)),
        toset(keys(var.accelerator_pool_contract.pools)),
      )) == 0 &&
      alltrue([
        for pool_id, bounds in var.accelerator_pool_contract.capacity_ownership.requested_overrides :
        floor(bounds.min_nodes) == bounds.min_nodes &&
        floor(bounds.max_nodes) == bounds.max_nodes &&
        bounds.min_nodes >= 0 &&
        bounds.max_nodes >= bounds.min_nodes &&
        var.accelerator_pool_contract.pools[pool_id].capacity.source == "operator-override" &&
        var.accelerator_pool_contract.pools[pool_id].capacity.min_nodes == bounds.min_nodes &&
        var.accelerator_pool_contract.pools[pool_id].capacity.max_nodes == bounds.max_nodes
      ]) &&
      alltrue([
        for pool_id, pool in var.accelerator_pool_contract.pools :
        (pool.capacity.source == "operator-override") == contains(
          keys(var.accelerator_pool_contract.capacity_ownership.requested_overrides),
          pool_id,
        )
      ]),
      false,
    )
    error_message = "accelerator_pool_contract capacity ownership, override digest, override keys, and effective bounds must agree exactly."
  }

  validation {
    condition = try(
      alltrue([
        for pool_id, pool in var.accelerator_pool_contract.pools :
        pool.id == pool_id &&
        can(regex("^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$", pool_id)) &&
        (var.accelerator_pool_contract.profile == "custom" ? (
          pool.state == "customer-specified" &&
          pool.evidence.hardware_state == "live-preflight-required"
          ) : (
          pool.state == "hardware-validated" &&
          pool.evidence.hardware_state == "hardware-validated"
        )) &&
        pool.provider.name == "nebius" &&
        contains(["provider-managed", "gpu-operator"], pool.provider.driver.owner) &&
        pool.resource_api.mode == "extended-resource" &&
        length(trimspace(pool.resource_api.resource_name)) > 0 &&
        floor(pool.node.gpus_per_node) == pool.node.gpus_per_node &&
        pool.node.gpus_per_node >= 1 &&
        (var.accelerator_pool_contract.profile == "custom" || (
          floor(pool.node.vcpu_count) == pool.node.vcpu_count &&
          pool.node.vcpu_count >= 1 &&
          floor(pool.node.memory_gib) == pool.node.memory_gib &&
          pool.node.memory_gib >= 1
        )) &&
        length(pool.node.host_architectures) >= 1 &&
        floor(pool.capacity.min_nodes) == pool.capacity.min_nodes &&
        floor(pool.capacity.max_nodes) == pool.capacity.max_nodes &&
        pool.capacity.min_nodes >= 0 &&
        pool.capacity.max_nodes >= max(pool.capacity.min_nodes, 1) &&
        contains(["regular", "preemptible"], pool.capacity.type) &&
        contains(["profile", "operator-override", "customer-tfvars"], pool.capacity.source) &&
        (pool.capacity.min_nodes > 0 || pool.capacity.scale_from_zero) &&
        (pool.capacity.source == "operator-override" || (
          pool.capacity.min_nodes == pool.capacity.profile_bounds.min_nodes &&
          pool.capacity.max_nodes == pool.capacity.profile_bounds.max_nodes
        )) &&
        pool.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == pool_id &&
        pool.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == pool.accelerator_class &&
        length([
          for availability in pool.region_availability : availability
          if availability.region == var.accelerator_pool_contract.target_region &&
          contains(["hardware-validated", "live-preflight-required"], availability.state) &&
          contains(availability.capacity_modes, pool.capacity.type)
        ]) == 1
      ]) &&
      (var.accelerator_pool_contract.profile == "custom" || (
        toset(keys(var.accelerator_pool_contract.pools)) == toset(keys(
          jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles[var.accelerator_pool_contract.profile].pools
        )) &&
        alltrue([
          for pool_id, pool in var.accelerator_pool_contract.pools :
          pool.capacity.profile_bounds.max_nodes == jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles[var.accelerator_pool_contract.profile].pools[pool_id].max_nodes &&
          pool.capacity.profile_bounds.min_nodes == try(
            jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles[var.accelerator_pool_contract.profile].pools[pool_id].floor_nodes[var.accelerator_pool_contract.floor_profile],
            -1,
          )
      ]))) &&
      length(distinct([
        for pool in values(var.accelerator_pool_contract.pools) : pool.scheduling.resource_flavor_name
      ])) == length(var.accelerator_pool_contract.pools),
      false,
    )
    error_message = "Every accelerator pool must be a unique, hardware-validated Nebius extended-resource realization with exact labels, integral capacity, and one validated target-region binding."
  }

  validation {
    condition = try(
      can(regex("^registry-[a-z0-9]+$", var.accelerator_pool_contract.artifact_source.registry.id)) &&
      can(regex("^project-[a-z0-9]+$", var.accelerator_pool_contract.artifact_source.registry.project_id)) &&
      length(trimspace(var.accelerator_pool_contract.artifact_source.registry.project_name)) > 0 &&
      length(trimspace(var.accelerator_pool_contract.artifact_source.registry.region)) > 0 &&
      length(trimspace(var.accelerator_pool_contract.artifact_source.registry.fqdn)) > 0 &&
      can(regex("^[0-9a-f]{64}$", var.accelerator_pool_contract.artifact_source.closure_sha256)) &&
      var.accelerator_pool_contract.artifact_source.cross_region_pull_required == (
        var.accelerator_pool_contract.target_region != var.accelerator_pool_contract.artifact_source.registry.region
      ),
      false,
    )
    error_message = "accelerator_pool_contract must bind a valid artifact registry, closure digest, and exact cross-region pull decision."
  }
}

locals {
  accelerator_pool_contract_sha256 = sha256(jsonencode(var.accelerator_pool_contract))
  accelerator_pool_ids             = sort(keys(var.accelerator_pool_contract.pools))
  accelerator_pool_capacity_view = {
    for pool_id, pool in var.accelerator_pool_contract.pools : pool_id => {
      accelerator_class = pool.accelerator_class
      resource_name     = pool.resource_api.resource_name
      resource_flavor   = pool.scheduling.resource_flavor_name
      capacity_type     = pool.capacity.type
      min_nodes         = pool.capacity.min_nodes
      max_nodes         = pool.capacity.max_nodes
      gpus_per_node     = pool.node.gpus_per_node
    }
  }
  accelerator_pool_maximum_gpus = sum([
    for pool in values(var.accelerator_pool_contract.pools) : pool.node.gpus_per_node * pool.capacity.max_nodes
  ])
  accelerator_pool_minimum_gpus = sum([
    for pool in values(var.accelerator_pool_contract.pools) : pool.node.gpus_per_node * pool.capacity.min_nodes
  ])
}
