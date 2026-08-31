variable "deployment" {
  description = <<-EOT
    Complete customer-facing FS2 deployment configuration. Hardware and model
    IDs resolve through checked-in qualification catalogs; provider facts,
    chart pins, generated cluster identities, and credentials are intentionally
    not customer inputs.
  EOT

  type = object({
    schema_version = optional(number, 1)
    name           = string

    profiles = optional(object({
      capacity     = optional(string, "minimal")
      accelerators = optional(string)
      models       = optional(string, "minimal")
    }), {})

    target = object({
      project_id   = string
      project_name = optional(string)
      region       = string
      network = optional(object({
        network_name        = optional(string)
        subnet_name         = optional(string)
        private_subnet_cidr = optional(string)
      }), {})
      system_update_strategy = optional(object({
        max_surge       = number
        max_unavailable = number
      }))
    })

    cluster = optional(object({
      kubernetes_version          = optional(string, "1.35")
      control_plane_allowed_cidrs = optional(set(string), [])
      system_pool = optional(object({
        capacity        = optional(string, "regular")
        platform        = optional(string, "cpu-d3")
        preset          = optional(string, "8vcpu-32gb")
        node_count      = optional(number)
        boot_disk_type  = optional(string, "NETWORK_SSD")
        boot_disk_gib   = optional(number, 160)
        max_surge       = optional(number)
        max_unavailable = optional(number)
        drain_timeout   = optional(string, "15m")
      }))
    }), {})

    accelerator_pool_capacity = optional(map(object({
      min_nodes = number
      max_nodes = number
    })), {})

    accelerator_pools = optional(map(object({
      platform          = string
      preset            = string
      accelerator_class = string
      gpus_per_node     = number
      resource_name     = optional(string, "nvidia.com/gpu")
      gpu_memory_gb     = optional(number)
      host_architecture = optional(string, "amd64")
      capacity_type     = optional(string, "preemptible")
      min_nodes         = optional(number, 0)
      max_nodes         = optional(number, 1)
      os                = optional(string, "ubuntu24.04")
      driver = optional(object({
        mode   = optional(string, "managed")
        preset = optional(string)
      }), {})
      boot_disk = optional(object({
        type     = optional(string, "NETWORK_SSD")
        size_gib = optional(number, 320)
      }), {})
      local_nvme        = optional(bool, false)
      local_nvme_mode   = optional(string, "raw")
      shared_filesystem = optional(bool, true)
      drain_timeout     = optional(string, "30m")
      topology = optional(object({
        mode              = optional(string, "standalone")
        infiniband_fabric = optional(string)
        rack_count        = optional(number, 0)
        nodes_per_rack    = optional(number, 18)
      }), {})
      mig = optional(object({
        strategy = optional(string, "none")
        config   = optional(string)
      }), {})
    })), {})

    models = optional(object({
      selection       = optional(string, "profile")
      enabled         = optional(set(string), [])
      image_overrides = optional(map(string), {})
      pool_overrides  = optional(map(string), {})
      scaling = optional(object({
        mode                       = optional(string, "keda")
        hot                        = optional(set(string), [])
        polling_interval_seconds   = optional(number, 5)
        cooldown_period_seconds    = optional(number, 300)
        fallback_failure_threshold = optional(number, 3)
        overrides = optional(map(object({
          min_replicas             = number
          max_replicas             = number
          target_queue_depth       = number
          polling_interval_seconds = number
          cooldown_seconds         = number
        })), {})
      }), {})
      cold_start_keepers = optional(bool, true)
    }), {})

    storage = optional(object({
      shared_cache = optional(object({
        size_gib         = optional(number)
        type             = optional(string, "NETWORK_SSD")
        block_size_bytes = optional(number, 4096)
        forbid_deletion  = optional(bool, false)
      }))
    }), {})

    artifacts = optional(object({
      external_registry_ids = optional(set(string), [])
    }), {})

    edge = optional(object({
      mode         = optional(string, "internal-only")
      source_cidrs = optional(set(string), [])
      acme_email   = optional(string)
      port_forward_ports = optional(object({
        control_plane  = optional(number, 18080)
        admin_console  = optional(number, 18081)
        operator_proxy = optional(number, 18082)
      }), {})
    }), {})

    observability = optional(object({
      dcgm_cold_start_campaign = optional(bool, false)
      grafana = optional(object({
        publish_external = optional(bool, false)
      }), {})
    }), {})

    applications = object({
      control_plane = object({
        repository             = string
        digest                 = string
        catalog_rollout_digest = string
      })
      admin_console = object({
        repository = string
        digest     = string
        provenance = object({
          source_commit = string
          source_tree   = string
          sbom_sha256   = string
          sbom_format   = optional(string, "cyclonedx-json")
        })
        replica_count = optional(number, 2)
      })
    })

    secrets = optional(object({
      grafana_admin_secret = optional(object({
        name         = optional(string, "fs2-grafana-admin")
        user_key     = optional(string, "admin-user")
        password_key = optional(string, "admin-password")
      }), {})
      grafana_username_env  = optional(string, "FS2_GRAFANA_ADMIN_USERNAME")
      grafana_password_env  = optional(string, "FS2_GRAFANA_ADMIN_PASSWORD")
      ngc_api_key_env       = optional(string, "FS2_NGC_API_KEY")
      nvcr_dockerconfig_env = optional(string, "FS2_NVCR_DOCKERCONFIGJSON")
    }), {})

    acceptance = optional(object({
      create_probe_job = optional(bool, false)
    }), {})
  })

  nullable = false

  validation {
    condition     = var.deployment.schema_version == 1
    error_message = "deployment.schema_version must be 1."
  }

  validation {
    condition = (
      can(regex("^[a-zA-Z0-9._:/-]+$", var.deployment.applications.control_plane.repository)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.control_plane.digest)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.control_plane.catalog_rollout_digest)) &&
      can(regex("^[a-zA-Z0-9._:/-]+$", var.deployment.applications.admin_console.repository)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.admin_console.digest)) &&
      can(regex("^[0-9a-f]{40}$", var.deployment.applications.admin_console.provenance.source_commit)) &&
      can(regex("^[0-9a-f]{40}$", var.deployment.applications.admin_console.provenance.source_tree)) &&
      can(regex("^[0-9a-f]{64}$", var.deployment.applications.admin_console.provenance.sbom_sha256)) &&
      var.deployment.applications.admin_console.provenance.sbom_format == "cyclonedx-json" &&
      floor(var.deployment.applications.admin_console.replica_count) == var.deployment.applications.admin_console.replica_count &&
      var.deployment.applications.admin_console.replica_count >= 1
    )
    error_message = "applications must provide immutable control-plane and admin-console OCI repositories/digests plus provenance."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,39}$", var.deployment.name))
    error_message = "deployment.name must be 5-40 lowercase alphanumeric or hyphen characters and start with a letter."
  }

  validation {
    condition = try(
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/approved-targets.json")).targets), var.deployment.target.project_id) ?
      jsondecode(file("${path.module}/catalog/profiles/approved-targets.json")).targets[var.deployment.target.project_id].region == var.deployment.target.region :
      (
        var.deployment.target.project_name != null &&
        var.deployment.target.network.network_name != null &&
        var.deployment.target.network.subnet_name != null &&
        can(cidrhost(var.deployment.target.network.private_subnet_cidr, 0)) &&
        var.deployment.target.system_update_strategy != null
      ),
      false,
    )
    error_message = "A catalog target needs the catalog region; a new target must provide project_name, network/subnet/CIDR, and update strategy for provider verification."
  }

  validation {
    condition = alltrue([
      for cidr in var.deployment.cluster.control_plane_allowed_cidrs : can(cidrhost(cidr, 0))
    ])
    error_message = "Every cluster.control_plane_allowed_cidrs entry must be a valid CIDR."
  }

  validation {
    condition = var.deployment.target.system_update_strategy == null || try(
      floor(var.deployment.target.system_update_strategy.max_surge) == var.deployment.target.system_update_strategy.max_surge &&
      floor(var.deployment.target.system_update_strategy.max_unavailable) == var.deployment.target.system_update_strategy.max_unavailable &&
      var.deployment.target.system_update_strategy.max_surge >= 0 &&
      var.deployment.target.system_update_strategy.max_unavailable >= 0 &&
      var.deployment.target.system_update_strategy.max_surge + var.deployment.target.system_update_strategy.max_unavailable >= 1,
      false,
    )
    error_message = "target.system_update_strategy must use nonnegative whole values with at least one of max_surge or max_unavailable nonzero."
  }

  validation {
    condition = var.deployment.cluster.system_pool == null || try(
      var.deployment.cluster.system_pool.capacity == "regular" &&
      can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.cluster.system_pool.platform)) &&
      can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.cluster.system_pool.preset)) &&
      (
        var.deployment.cluster.system_pool.node_count == null ||
        (
          floor(var.deployment.cluster.system_pool.node_count) == var.deployment.cluster.system_pool.node_count &&
          var.deployment.cluster.system_pool.node_count >= 1 &&
          var.deployment.cluster.system_pool.node_count <= 32
        )
      ) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.deployment.cluster.system_pool.boot_disk_type) &&
      floor(var.deployment.cluster.system_pool.boot_disk_gib) == var.deployment.cluster.system_pool.boot_disk_gib &&
      var.deployment.cluster.system_pool.boot_disk_gib >= 32 &&
      var.deployment.cluster.system_pool.boot_disk_gib <= 4096 &&
      (
        var.deployment.cluster.system_pool.max_surge == null ||
        floor(var.deployment.cluster.system_pool.max_surge) == var.deployment.cluster.system_pool.max_surge
        && var.deployment.cluster.system_pool.max_surge >= 0
      ) &&
      (
        var.deployment.cluster.system_pool.max_unavailable == null ||
        floor(var.deployment.cluster.system_pool.max_unavailable) == var.deployment.cluster.system_pool.max_unavailable
        && var.deployment.cluster.system_pool.max_unavailable >= 0
      ) &&
      (
        var.deployment.cluster.system_pool.max_surge == null ||
        var.deployment.cluster.system_pool.max_unavailable == null ||
        var.deployment.cluster.system_pool.max_surge + var.deployment.cluster.system_pool.max_unavailable >= 1
      ) &&
      can(regex("^[1-9][0-9]*m$", var.deployment.cluster.system_pool.drain_timeout)),
      false,
    )
    error_message = "cluster.system_pool must match the bounded regular CPU-pool, disk, rollout, and drain-time contract consumed by infrastructure."
  }

  validation {
    condition = var.deployment.storage.shared_cache == null || try(
      (
        var.deployment.storage.shared_cache.size_gib == null ||
        (
          floor(var.deployment.storage.shared_cache.size_gib) == var.deployment.storage.shared_cache.size_gib &&
          var.deployment.storage.shared_cache.size_gib >= 32 &&
          var.deployment.storage.shared_cache.size_gib <= 65536
        )
      ) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED", "NETWORK_SSD_IO_M3"], var.deployment.storage.shared_cache.type) &&
      contains([4096, 8192, 16384, 32768, 65536], var.deployment.storage.shared_cache.block_size_bytes),
      false,
    )
    error_message = "storage.shared_cache must use a positive whole-GiB size and a supported network filesystem type/block size."
  }

  validation {
    condition = alltrue([
      for registry_id in var.deployment.artifacts.external_registry_ids :
      can(regex("^registry-[a-z0-9]+$", registry_id))
    ])
    error_message = "artifacts.external_registry_ids must contain only Nebius registry IDs."
  }

  validation {
    condition = (
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/capacity-profiles.json")).capacity_profiles), var.deployment.profiles.capacity) &&
      contains(
        keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles),
        coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity),
      ) &&
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles), var.deployment.profiles.models)
    )
    error_message = "Each deployment.profiles selection must exist in its corresponding capacity, accelerator-pool, or model catalog."
  }

  validation {
    condition = try(alltrue([
      for pool_id, bounds in var.deployment.accelerator_pool_capacity : (
        contains(
          keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools),
          pool_id,
        ) &&
        floor(bounds.min_nodes) == bounds.min_nodes &&
        floor(bounds.max_nodes) == bounds.max_nodes &&
        bounds.min_nodes >= 0 &&
        bounds.max_nodes >= 1 &&
        bounds.max_nodes >= bounds.min_nodes &&
        bounds.max_nodes <= jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools[pool_id].max_nodes
      )
    ]), false)
    error_message = "accelerator_pool_capacity must use pool IDs in the selected profile and satisfy 0 <= min_nodes <= max_nodes <= the reviewed ceiling."
  }

  validation {
    condition = try(alltrue([
      for pool_id, pool in var.deployment.accelerator_pools : (
        can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", pool_id)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.preset)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$", pool.accelerator_class)) &&
        floor(pool.gpus_per_node) == pool.gpus_per_node && pool.gpus_per_node >= 1 &&
        contains(["amd64", "arm64"], pool.host_architecture) &&
        contains(["regular", "preemptible"], pool.capacity_type) &&
        contains(["raw", "kubelet-ephemeral"], pool.local_nvme_mode) &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], pool.boot_disk.type) &&
        floor(pool.boot_disk.size_gib) == pool.boot_disk.size_gib &&
        pool.boot_disk.size_gib >= 32 && pool.boot_disk.size_gib <= 4096 &&
        floor(pool.min_nodes) == pool.min_nodes && pool.min_nodes >= 0 &&
        floor(pool.max_nodes) == pool.max_nodes && pool.max_nodes >= pool.min_nodes &&
        contains(["managed", "operator"], pool.driver.mode) &&
        (pool.driver.mode == "managed" ? pool.driver.preset != null : pool.driver.preset == null) &&
        contains(["standalone", "gpu_cluster", "nvlink_rack"], pool.topology.mode) &&
        (pool.topology.mode == "gpu_cluster" ? pool.topology.infiniband_fabric != null : true) &&
        (pool.topology.mode == "nvlink_rack" ? (
          pool.capacity_type == "regular" &&
          pool.min_nodes == pool.max_nodes &&
          pool.topology.rack_count >= 1 &&
          pool.topology.nodes_per_rack >= 1 &&
          pool.max_nodes == pool.topology.rack_count * pool.topology.nodes_per_rack
        ) : pool.topology.rack_count == 0) &&
        contains(["none", "single", "mixed"], pool.mig.strategy) &&
        (pool.mig.strategy == "none" || (
          pool.driver.mode == "operator" &&
          pool.mig.config != null &&
          pool.resource_name != "nvidia.com/gpu"
        ))
      )
    ]), false)
    error_message = "accelerator_pools must be structurally valid provider pools, including a whole 32-4096 GiB supported boot disk; platform and preset stay open-ended so current and future Nebius GPUs pass through to provider validation."
  }

  validation {
    condition = try(
      contains(["profile", "explicit"], var.deployment.models.selection) &&
      (var.deployment.models.selection == "profile" ? length(var.deployment.models.enabled) == 0 : true) &&
      length(setsubtract(
        var.deployment.models.enabled,
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0,
      false,
    )
    error_message = "models.selection must be profile or explicit; profile requires an empty enabled set, while explicit IDs must belong to the model profile."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.image_overrides)),
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      alltrue([
        for image in values(var.deployment.models.image_overrides) :
        can(regex("^[a-zA-Z0-9._:/@-]+$", image)) && !endswith(split("/", image)[0], ".invalid")
      ]),
      false,
    )
    error_message = "models.image_overrides keys must belong to the selected model profile and values must be non-placeholder OCI image references."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.pool_overrides)),
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      alltrue([
        for pool_id in values(var.deployment.models.pool_overrides) : contains(
          length(var.deployment.accelerator_pools) > 0 ?
          toset(keys(var.deployment.accelerator_pools)) :
          toset(keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools)),
          pool_id,
        )
      ]),
      false,
    )
    error_message = "models.pool_overrides must map selected-profile model IDs to accelerator pools declared by this deployment."
  }

  validation {
    condition = try(
      length(setsubtract(
        var.deployment.models.scaling.hot,
        var.deployment.models.selection == "profile" ?
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes) :
        var.deployment.models.enabled,
      )) == 0,
      false,
    )
    error_message = "models.scaling.hot must be a subset of the enabled model set."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.scaling.overrides)),
        var.deployment.models.selection == "profile" ?
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes) :
        var.deployment.models.enabled,
      )) == 0 &&
      alltrue([
        for scaling in values(var.deployment.models.scaling.overrides) : (
          floor(scaling.min_replicas) == scaling.min_replicas &&
          floor(scaling.max_replicas) == scaling.max_replicas &&
          scaling.min_replicas >= 0 &&
          scaling.max_replicas >= scaling.min_replicas &&
          scaling.max_replicas <= 128 &&
          scaling.target_queue_depth >= 1 &&
          scaling.polling_interval_seconds >= 1 &&
          scaling.polling_interval_seconds <= 60 &&
          scaling.cooldown_seconds >= 5 &&
          scaling.cooldown_seconds <= 7200
        )
      ]),
      false,
    )
    error_message = "models.scaling.overrides must target enabled models and use bounded KEDA settings; the deployment contract further limits replicas to compatible accelerator capacity."
  }

  validation {
    condition = (
      contains(["static", "keda"], var.deployment.models.scaling.mode) &&
      (var.deployment.models.scaling.mode == "keda" || length(var.deployment.models.scaling.hot) == 0) &&
      floor(var.deployment.models.scaling.polling_interval_seconds) == var.deployment.models.scaling.polling_interval_seconds &&
      var.deployment.models.scaling.polling_interval_seconds >= 1 &&
      var.deployment.models.scaling.polling_interval_seconds <= 60 &&
      floor(var.deployment.models.scaling.cooldown_period_seconds) == var.deployment.models.scaling.cooldown_period_seconds &&
      var.deployment.models.scaling.cooldown_period_seconds >= 5 &&
      var.deployment.models.scaling.cooldown_period_seconds <= 7200 &&
      floor(var.deployment.models.scaling.fallback_failure_threshold) == var.deployment.models.scaling.fallback_failure_threshold &&
      var.deployment.models.scaling.fallback_failure_threshold >= 1 &&
      var.deployment.models.scaling.fallback_failure_threshold <= 20
    )
    error_message = "models.scaling contains an invalid mode, hot floor, polling, cooldown, or fallback value."
  }

  validation {
    condition = (
      (!var.deployment.observability.dcgm_cold_start_campaign || var.deployment.profiles.models == "full_catalog") &&
      (!var.deployment.acceptance.create_probe_job || length(var.deployment.models.enabled) > 0 || var.deployment.models.selection == "profile")
    )
    error_message = "The DCGM cold-start campaign requires the full_catalog model profile, and an acceptance probe requires at least one selected model."
  }

  validation {
    condition = (
      contains(["public", "internal-only"], var.deployment.edge.mode) &&
      (
        var.deployment.edge.mode == "public" ?
        length(var.deployment.edge.source_cidrs) > 0 &&
        length(var.deployment.edge.source_cidrs) <= 8 &&
        alltrue([
          for cidr in var.deployment.edge.source_cidrs :
          can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$", cidr)) && can(cidrhost(cidr, 0))
        ]) &&
        var.deployment.edge.acme_email != null && can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.deployment.edge.acme_email)) :
        length(var.deployment.edge.source_cidrs) == 0 && var.deployment.edge.acme_email == null
      )
    )
    error_message = "Public edge mode requires one to eight IPv4 source CIDRs and an ACME email; internal-only mode requires neither."
  }

  validation {
    condition = (
      alltrue([
        for port in values(var.deployment.edge.port_forward_ports) :
        floor(port) == port && port >= 1024 && port <= 65535
      ]) &&
      length(toset(values(var.deployment.edge.port_forward_ports))) == 3
    )
    error_message = "edge.port_forward_ports must contain three distinct whole TCP ports from 1024 through 65535."
  }

  validation {
    condition = alltrue([
      for name in [
        var.deployment.secrets.grafana_username_env,
        var.deployment.secrets.grafana_password_env,
        var.deployment.secrets.ngc_api_key_env,
        var.deployment.secrets.nvcr_dockerconfig_env,
      ] : can(regex("^[A-Z][A-Z0-9_]{2,127}$", name))
    ])
    error_message = "Secret environment-variable references must be uppercase shell variable names."
  }
}
