variable "nebius_profile" {
  description = "Existing authenticated Nebius CLI profile name. The wrapper supplies it; credentials never enter Terraform configuration or state."
  type        = string
  default     = "sandbox"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", var.nebius_profile))
    error_message = "nebius_profile must be a nonempty, bounded CLI profile name containing only letters, digits, dot, underscore, or hyphen."
  }
}

variable "project_id" {
  description = "Exact target project ID, supplied through an external mode-0600 tfvars file. Without target_binding it must remain in the checked-in legacy target catalog."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition = (
      var.target_binding != null ||
      contains(
        keys(jsondecode(file("${path.module}/../../catalog/profiles/approved-targets.json")).targets),
        nonsensitive(var.project_id),
      )
    )
    error_message = "project_id must select a checked-in legacy target unless an explicit target_binding is supplied."
  }
}

variable "target_binding" {
  description = <<-EOT
    Optional generated target assertion for a project that is not in the
    legacy approved-targets catalog. Provider data must independently confirm
    the exact project, region, READY network, READY subnet, and sole private
    CIDR before any managed resource can be planned. Accelerator qualification
    remains exclusively checked-in profile data and cannot be asserted here.
  EOT
  type = object({
    project_id          = string
    project_name        = optional(string)
    region              = string
    network_name        = string
    subnet_name         = string
    private_subnet_cidr = string
    system_update_strategy = object({
      max_surge       = number
      max_unavailable = number
    })
  })
  default  = null
  nullable = true

  validation {
    condition = var.target_binding == null || try(
      can(regex("^project-[a-z0-9]+$", var.target_binding.project_id)) &&
      (var.target_binding.project_name == null ? true : length(trimspace(var.target_binding.project_name)) > 0) &&
      can(regex("^[a-z][a-z0-9-]{1,31}[a-z0-9]$", var.target_binding.region)) &&
      length(trimspace(var.target_binding.network_name)) > 0 &&
      length(trimspace(var.target_binding.subnet_name)) > 0 &&
      can(cidrhost(var.target_binding.private_subnet_cidr, 0)) &&
      var.target_binding.private_subnet_cidr != "0.0.0.0/0" &&
      floor(var.target_binding.system_update_strategy.max_surge) == var.target_binding.system_update_strategy.max_surge &&
      floor(var.target_binding.system_update_strategy.max_unavailable) == var.target_binding.system_update_strategy.max_unavailable &&
      var.target_binding.system_update_strategy.max_surge >= 0 &&
      var.target_binding.system_update_strategy.max_unavailable >= 0 &&
      var.target_binding.system_update_strategy.max_surge + var.target_binding.system_update_strategy.max_unavailable >= 1,
      false,
    )
    error_message = "target_binding must match project_id and contain a bounded region, nonempty network/subnet, restricted CIDR, and an integral nonzero rollout allowance."
  }
}

variable "source_commit" {
  description = "Exact full Git commit ID reviewed for this disposable lifecycle. It is embedded in the cross-stage infrastructure contract."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_commit))
    error_message = "source_commit must be the exact lowercase 40-character Git commit ID reviewed for this run."
  }
}

variable "public_edge_mode" {
  description = "Edge exposure for this lifecycle. public provisions the existing allocated IPv4 edge; internal-only provisions no public allocation and is accepted only through a bounded kubectl port-forward."
  type        = string
  default     = "public"

  validation {
    condition     = contains(["public", "internal-only"], var.public_edge_mode)
    error_message = "public_edge_mode must be public or internal-only."
  }
}

variable "public_edge_source_cidrs" {
  description = "IPv4 client CIDRs admitted to the temporary authenticated public edge."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition = (
      (var.public_edge_mode == "public" && length(var.public_edge_source_cidrs) > 0 && length(var.public_edge_source_cidrs) <= 8) ||
      (var.public_edge_mode == "internal-only" && length(var.public_edge_source_cidrs) == 0)
      ) && alltrue([
        for cidr in var.public_edge_source_cidrs : can(cidrhost(cidr, 0)) && can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "public mode requires one to eight valid IPv4 CIDRs; internal-only mode requires an empty public_edge_source_cidrs list."
  }
}

variable "port_forward_local_ports" {
  description = "Distinct non-privileged loopback ports for the internal-only control-plane, admin-console, and same-origin operator proxy."
  type = object({
    control_plane  = number
    admin_console  = number
    operator_proxy = number
  })
  default = {
    control_plane  = 18080
    admin_console  = 18081
    operator_proxy = 18082
  }

  validation {
    condition = (
      alltrue([
        for port in values(var.port_forward_local_ports) :
        floor(port) == port && port >= 1024 && port <= 65535
      ]) &&
      length(toset(values(var.port_forward_local_ports))) == 3
    )
    error_message = "port_forward_local_ports must contain three distinct whole TCP ports from 1024 through 65535."
  }
}

variable "public_edge_service_ports" {
  description = "Envoy listener, shifted target, and pinned NodePort contract shared with the control-plane chart."
  type = object({
    http = object({
      listener_port = number
      target_port   = number
      node_port     = number
    })
    https = object({
      listener_port = number
      target_port   = number
      node_port     = number
    })
  })
  default = {
    http = {
      listener_port = 80
      target_port   = 10080
      node_port     = 31425
    }
    https = {
      listener_port = 443
      target_port   = 10443
      node_port     = 32633
    }
  }

  validation {
    condition = (
      var.public_edge_service_ports.http.listener_port == 80 &&
      var.public_edge_service_ports.http.target_port == 10080 &&
      var.public_edge_service_ports.https.listener_port == 443 &&
      var.public_edge_service_ports.https.target_port == 10443 &&
      var.public_edge_service_ports.http.node_port >= 30000 &&
      var.public_edge_service_ports.http.node_port <= 32767 &&
      var.public_edge_service_ports.https.node_port >= 30000 &&
      var.public_edge_service_ports.https.node_port <= 32767 &&
      var.public_edge_service_ports.http.node_port != var.public_edge_service_ports.https.node_port
    )
    error_message = "public_edge_service_ports differs from the reviewed Envoy/Nebius mapping."
  }
}

variable "run_id" {
  description = "Unique lowercase lifecycle identifier. It is embedded in every task-owned resource name and label."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{5,11}$", var.run_id))
    error_message = "run_id must be 6-12 lowercase alphanumeric characters and start with a letter."
  }
}

variable "name_prefix" {
  description = "Fixed namespace separating disposable resources from the retained platform."
  type        = string
  default     = "fs2-disposable"

  validation {
    condition     = var.name_prefix == "fs2-disposable"
    error_message = "name_prefix is fixed so disposable resources cannot collide with retained resources."
  }
}

variable "cluster_name" {
  description = "Optional explicit cluster/resource name. Null preserves the legacy <name_prefix>-<run_id> name and all existing resource identities."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = var.cluster_name == null ? true : (
      length(var.cluster_name) >= 5 &&
      length(var.cluster_name) <= 40 &&
      can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.cluster_name))
    )
    error_message = "cluster_name must be null or a 5-40 character lowercase DNS-style name without a trailing hyphen."
  }
}

variable "kubernetes_version" {
  description = "Managed Kubernetes minor. Live MK8s compatibility validation remains authoritative for each accelerator platform."
  type        = string
  default     = "1.35"

  validation {
    condition     = can(regex("^1\\.(3[1-9]|[4-9][0-9])$", var.kubernetes_version))
    error_message = "kubernetes_version must be a supported 1.31-or-newer minor; verify the selected GPU with the live MK8s compatibility matrix."
  }
}

variable "control_plane_allowed_cidrs" {
  description = "Optional public API allowlist. Empty relies on Nebius authentication during the short validation lifecycle."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.control_plane_allowed_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every control-plane allowlist entry must be a valid CIDR."
  }
}

variable "system_pool" {
  description = <<-EOT
    Optional typed CPU system-pool override. Null preserves the selected
    capacity profile, target rollout strategy, cpu-d3/8vcpu-32gb shape, and
    160 GiB NETWORK_SSD boot disk. System nodes remain regular in this release
    so the control plane add-ons are not coupled to preemptible GPU capacity.
  EOT
  type = object({
    capacity        = optional(string, "regular")
    platform        = optional(string, "cpu-d3")
    preset          = optional(string, "8vcpu-32gb")
    node_count      = optional(number)
    boot_disk_type  = optional(string, "NETWORK_SSD")
    boot_disk_gib   = optional(number, 160)
    max_surge       = optional(number)
    max_unavailable = optional(number)
    drain_timeout   = optional(string, "15m")
  })
  default  = null
  nullable = true

  validation {
    condition = var.system_pool == null || try(
      var.system_pool.capacity == "regular" &&
      can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", var.system_pool.platform)) &&
      can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", var.system_pool.preset)) &&
      (var.system_pool.node_count == null ? true : (
        floor(var.system_pool.node_count) == var.system_pool.node_count &&
        var.system_pool.node_count >= 1 &&
        var.system_pool.node_count <= 32
      )) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.system_pool.boot_disk_type) &&
      floor(var.system_pool.boot_disk_gib) == var.system_pool.boot_disk_gib &&
      var.system_pool.boot_disk_gib >= 32 &&
      var.system_pool.boot_disk_gib <= 4096 &&
      (var.system_pool.max_surge == null ? true : (
        floor(var.system_pool.max_surge) == var.system_pool.max_surge &&
        var.system_pool.max_surge >= 0
      )) &&
      (var.system_pool.max_unavailable == null ? true : (
        floor(var.system_pool.max_unavailable) == var.system_pool.max_unavailable &&
        var.system_pool.max_unavailable >= 0
      )) &&
      can(regex("^[1-9][0-9]*m$", var.system_pool.drain_timeout)),
      false,
    )
    error_message = "system_pool must use regular capacity, a bounded provider shape/count/boot disk, and an integral nonzero rollout allowance."
  }
}

variable "shared_cache" {
  description = "Optional typed shared model-cache filesystem override. Null preserves the selected capacity profile size and the existing NETWORK_SSD/4096/deletable defaults."
  type = object({
    size_gib         = optional(number)
    type             = optional(string, "NETWORK_SSD")
    block_size_bytes = optional(number, 4096)
    forbid_deletion  = optional(bool, false)
  })
  default  = null
  nullable = true

  validation {
    condition = var.shared_cache == null || try(
      (var.shared_cache.size_gib == null ? true : (
        floor(var.shared_cache.size_gib) == var.shared_cache.size_gib &&
        var.shared_cache.size_gib >= 32 &&
        var.shared_cache.size_gib <= 65536
      )) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.shared_cache.type) &&
      contains([4096, 8192, 16384, 32768, 65536], var.shared_cache.block_size_bytes),
      false,
    )
    error_message = "shared_cache must use a bounded integral size, supported network disk type, and supported power-of-two block size."
  }
}

variable "reference_data" {
  description = "Dedicated same-region filesystem and versioned object storage for immutable scientific reference data. The portable default is disposable; retained storage is explicit opt-in."
  type = object({
    enabled = bool
    lifecycle = object({
      retention_mode = string
    })
    cpu_pool = object({
      platform   = string
      preset     = string
      node_count = number
      schedulable_capacity = object({
        cpu_millicores        = number
        memory_mib            = number
        ephemeral_storage_mib = number
      })
      boot_disk_type  = string
      boot_disk_gib   = number
      max_surge       = number
      max_unavailable = number
      drain_timeout   = string
    })
    filesystem = object({
      size_gib         = number
      type             = string
      block_size_bytes = number
      forbid_deletion  = bool
    })
    object_storage = object({
      bucket_name  = string
      max_size_gib = number
    })
  })
  default = {
    enabled = false
    lifecycle = {
      retention_mode = "disposable"
    }
    cpu_pool = {
      platform   = "cpu-d3"
      preset     = "8vcpu-32gb"
      node_count = 1
      schedulable_capacity = {
        cpu_millicores        = 7000
        memory_mib            = 28672
        ephemeral_storage_mib = 114688
      }
      boot_disk_type  = "NETWORK_SSD"
      boot_disk_gib   = 160
      max_surge       = 1
      max_unavailable = 0
      drain_timeout   = "15m"
    }
    filesystem = {
      size_gib         = 2048
      type             = "NETWORK_SSD"
      block_size_bytes = 4096
      forbid_deletion  = false
    }
    object_storage = {
      bucket_name  = "disabled-reference-data.invalid"
      max_size_gib = 2048
    }
  }

  validation {
    condition = try(
      !var.reference_data.enabled || (
        contains(["retain", "disposable"], var.reference_data.lifecycle.retention_mode) &&
        (
          var.reference_data.lifecycle.retention_mode == "retain" ?
          var.reference_data.filesystem.forbid_deletion :
          !var.reference_data.filesystem.forbid_deletion
        ) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", var.reference_data.cpu_pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", var.reference_data.cpu_pool.preset)) &&
        floor(var.reference_data.cpu_pool.node_count) == var.reference_data.cpu_pool.node_count &&
        var.reference_data.cpu_pool.node_count >= 1 &&
        var.reference_data.cpu_pool.node_count <= 32 &&
        floor(var.reference_data.cpu_pool.schedulable_capacity.cpu_millicores) == var.reference_data.cpu_pool.schedulable_capacity.cpu_millicores &&
        var.reference_data.cpu_pool.schedulable_capacity.cpu_millicores >= 1000 &&
        floor(var.reference_data.cpu_pool.schedulable_capacity.memory_mib) == var.reference_data.cpu_pool.schedulable_capacity.memory_mib &&
        var.reference_data.cpu_pool.schedulable_capacity.memory_mib >= 1024 &&
        floor(var.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib) == var.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib &&
        var.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib >= 1024 &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.reference_data.cpu_pool.boot_disk_type) &&
        floor(var.reference_data.cpu_pool.boot_disk_gib) == var.reference_data.cpu_pool.boot_disk_gib &&
        var.reference_data.cpu_pool.boot_disk_gib >= 32 &&
        var.reference_data.cpu_pool.boot_disk_gib <= 4096 &&
        floor(var.reference_data.cpu_pool.max_surge) == var.reference_data.cpu_pool.max_surge &&
        floor(var.reference_data.cpu_pool.max_unavailable) == var.reference_data.cpu_pool.max_unavailable &&
        var.reference_data.cpu_pool.max_surge >= 0 &&
        var.reference_data.cpu_pool.max_unavailable >= 0 &&
        var.reference_data.cpu_pool.max_surge + var.reference_data.cpu_pool.max_unavailable >= 1 &&
        can(regex("^[1-9][0-9]*m$", var.reference_data.cpu_pool.drain_timeout)) &&
        floor(var.reference_data.filesystem.size_gib) == var.reference_data.filesystem.size_gib &&
        var.reference_data.filesystem.size_gib >= 1611 &&
        var.reference_data.filesystem.size_gib <= 65536 &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.reference_data.filesystem.type) &&
        contains([4096, 8192, 16384, 32768, 65536], var.reference_data.filesystem.block_size_bytes) &&
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.reference_data.object_storage.bucket_name)) &&
        floor(var.reference_data.object_storage.max_size_gib) == var.reference_data.object_storage.max_size_gib &&
        var.reference_data.object_storage.max_size_gib >= 1611 &&
        var.reference_data.object_storage.max_size_gib <= 65536
      ),
      false,
    )
    error_message = "enabled reference_data requires the default disposable+deletable lifecycle or explicit retain+forbid_deletion semantics, a bounded dedicated CPU pool with conservative schedulable capacity, a valid bucket and dedicated filesystem/object capacity of 1611-65536 whole GiB."
  }
}

variable "scientific_artifacts" {
  description = "Dedicated same-region versioned object store for scientific result artifacts. It is a distinct bucket, identity and key from the reference-data plane and is disposable unless retention is explicitly requested."
  type = object({
    enabled = bool
    lifecycle = object({
      retention_mode = string
    })
    object_storage = object({
      bucket_name  = string
      max_size_gib = number
    })
    retention_days = number
  })
  default = {
    enabled = false
    lifecycle = {
      retention_mode = "disposable"
    }
    object_storage = {
      bucket_name  = "disabled-scientific-artifacts.invalid"
      max_size_gib = 4096
    }
    retention_days = 90
  }

  validation {
    condition = try(
      !var.scientific_artifacts.enabled || (
        contains(["retain", "disposable"], var.scientific_artifacts.lifecycle.retention_mode) &&
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.scientific_artifacts.object_storage.bucket_name)) &&
        floor(var.scientific_artifacts.object_storage.max_size_gib) == var.scientific_artifacts.object_storage.max_size_gib &&
        var.scientific_artifacts.object_storage.max_size_gib >= 16 &&
        var.scientific_artifacts.object_storage.max_size_gib <= 65536 &&
        floor(var.scientific_artifacts.retention_days) == var.scientific_artifacts.retention_days &&
        var.scientific_artifacts.retention_days >= 1 &&
        var.scientific_artifacts.retention_days <= 3650
      ),
      false,
    )
    error_message = "enabled scientific_artifacts requires an explicit retain or disposable lifecycle, a valid globally unique bucket name, 16-65536 whole GiB of capacity and a 1-3650 day application retention window."
  }
}

variable "capacity_profile" {
  description = "Reviewed capacity envelope. full_catalog supports every canonical route plus the second MSA backend without HCL edits."
  type        = string
  default     = "minimal"

  validation {
    condition = contains(
      keys(jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json")).capacity_profiles),
      var.capacity_profile,
    )
    error_message = "capacity_profile must exist in the system/cache capacity catalog."
  }
}

variable "accelerator_pool_profile" {
  description = "Optional independently selected checked-in accelerator pool profile. Null preserves the legacy behavior of using capacity_profile."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = contains(
      keys(jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")).profiles),
      coalesce(var.accelerator_pool_profile, var.capacity_profile),
    )
    error_message = "accelerator_pool_profile must be null or name a checked-in accelerator pool profile; tfvars cannot define provider or GPU qualification facts."
  }
}

variable "gpu_floor_profile" {
  description = "Reviewed preemptible GPU warm floor. zero preserves scale-from-zero; full_catalog keeps the complete 23-GPU topology hot."
  type        = string
  default     = "zero"

  validation {
    condition     = contains(["zero", "representative", "full_catalog"], var.gpu_floor_profile)
    error_message = "gpu_floor_profile must be zero, representative, or full_catalog."
  }
}

variable "accelerator_pool_capacity_overrides" {
  description = <<-EOT
    Optional capacity-only patch keyed by a stable pool ID from the selected
    accelerator-pool profile, for example
    { "pool-id" = { min_nodes = 0, max_nodes = 2 } }. Every entry must contain
    exactly min_nodes and max_nodes. Provider, accelerator, driver, storage,
    topology, resource API, labels, taints, capacity mode, and region facts
    remain profile-owned.
  EOT
  type        = map(map(number))
  default     = {}
  nullable    = false

  validation {
    condition = alltrue([
      for pool_id, bounds in var.accelerator_pool_capacity_overrides : (
        can(regex("^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$", pool_id)) &&
        toset(keys(bounds)) == toset(["min_nodes", "max_nodes"]) &&
        try(
          floor(bounds.min_nodes) == bounds.min_nodes &&
          floor(bounds.max_nodes) == bounds.max_nodes &&
          bounds.min_nodes >= 0 &&
          bounds.max_nodes >= bounds.min_nodes,
          false,
        )
      )
    ])
    error_message = "accelerator_pool_capacity_overrides must be a stable-ID map whose values contain exactly nonnegative integer min_nodes/max_nodes with min_nodes <= max_nodes."
  }
}

variable "custom_accelerator_pools" {
  description = <<-EOT
    Open-ended Nebius accelerator pools. Platform and preset are deliberately
    not allowlisted: the live project platform API and MK8s compatibility
    matrix are authoritative, so new GPU generations work without a library
    release. An empty map uses the checked-in, evidence-backed profile.
  EOT
  type = map(object({
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
    reservation_policy = optional(object({
      policy          = optional(string, "STRICT")
      reservation_ids = optional(list(string), [])
    }))
    os = optional(string, "ubuntu24.04")
    driver = optional(object({
      mode   = optional(string, "managed")
      preset = optional(string)
    }), {})
    boot_disk = optional(object({
      type     = optional(string, "NETWORK_SSD")
      size_gib = optional(number, 320)
    }), {})
    local_nvme                = optional(bool, false)
    local_nvme_mode           = optional(string, "raw")
    shared_filesystem         = optional(bool, true)
    reference_data_filesystem = optional(bool, false)
    drain_timeout             = optional(string, "30m")
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
  }))
  default  = {}
  nullable = false

  validation {
    condition = alltrue([
      for pool_id, pool in var.custom_accelerator_pools : (
        can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", pool_id)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.preset)) &&
        floor(pool.gpus_per_node) == pool.gpus_per_node && pool.gpus_per_node >= 1 &&
        try(length(trimspace(pool.resource_name)) > 0, false) &&
        contains(["amd64", "arm64"], pool.host_architecture) &&
        contains(["regular", "preemptible"], pool.capacity_type) &&
        contains(["raw", "kubelet-ephemeral"], pool.local_nvme_mode) &&
        floor(pool.min_nodes) == pool.min_nodes && pool.min_nodes >= 0 &&
        floor(pool.max_nodes) == pool.max_nodes && pool.max_nodes >= pool.min_nodes &&
        (pool.reservation_policy == null ? true : (
          pool.capacity_type == "regular" &&
          pool.min_nodes >= 1 &&
          pool.min_nodes == pool.max_nodes &&
          contains(["AUTO", "STRICT"], pool.reservation_policy.policy) &&
          length(pool.reservation_policy.reservation_ids) >= 1 &&
          length(distinct(pool.reservation_policy.reservation_ids)) == length(pool.reservation_policy.reservation_ids) &&
          alltrue([
            for reservation_id in pool.reservation_policy.reservation_ids :
            can(regex("^capacityblockgroup-[a-z0-9]+$", reservation_id))
          ])
        )) &&
        contains(["managed", "operator"], pool.driver.mode) &&
        (pool.driver.mode == "managed" ? try(length(trimspace(pool.driver.preset)) > 0, false) : pool.driver.preset == null) &&
        contains(["standalone", "gpu_cluster", "nvlink_rack"], pool.topology.mode) &&
        (pool.topology.mode == "standalone" ? pool.topology.infiniband_fabric == null : true) &&
        (pool.topology.mode == "gpu_cluster" ? try(length(trimspace(pool.topology.infiniband_fabric)) > 0, false) : true) &&
        (pool.topology.mode == "nvlink_rack" ? (
          pool.platform == "gpu-gb300" &&
          pool.preset == "4gpu-112vcpu-800gb" &&
          pool.accelerator_class == "nvidia-gb300" &&
          pool.gpus_per_node == 4 &&
          pool.host_architecture == "arm64" &&
          pool.capacity_type == "regular" &&
          pool.driver.mode == "managed" &&
          pool.mig.strategy == "none" &&
          pool.min_nodes == pool.max_nodes &&
          pool.topology.rack_count >= 1 &&
          pool.topology.nodes_per_rack == 18 &&
          (pool.topology.rack_count == 1 || try(length(trimspace(pool.topology.infiniband_fabric)) > 0, false)) &&
          pool.max_nodes == pool.topology.rack_count * pool.topology.nodes_per_rack
        ) : pool.topology.rack_count == 0) &&
        contains(["none", "single", "mixed"], pool.mig.strategy) &&
        (pool.mig.strategy == "none" || (
          pool.driver.mode == "operator" &&
          try(length(trimspace(pool.mig.config)) > 0, false)
        ))
      )
    ])
    error_message = "custom_accelerator_pools must satisfy sizing, driver, topology, architecture, MIG, and reservation invariants; reservations require fixed regular capacity, AUTO or STRICT policy, and unique capacity-block-group IDs; NVLink racks are fixed 18-node GB300 groups and multiple racks require a fabric; other platform/preset validity is checked live."
  }
}

variable "external_registry_ids" {
  description = "Optional same-tenant Nebius registries whose immutable application or model images are referenced by terraform.tfvars. Terraform creates one project-scoped reader group per registry; the run-scoped node identity receives viewer only."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for registry_id in var.external_registry_ids : can(regex("^registry-[a-z0-9]+$", registry_id))
    ])
    error_message = "external_registry_ids must contain only Nebius registry IDs."
  }
}

variable "registry_delivery" {
  description = "Non-secret artifact delivery policy and upstream hosts derived from the customer facade. Regional mirroring is executed by the wrapper after this stage creates the target registry."
  type = object({
    mode              = string
    repository_prefix = string
    source_hosts      = list(string)
  })
  default = {
    mode              = "regional-mirror"
    repository_prefix = ""
    source_hosts      = []
  }
  nullable = false

  validation {
    condition = (
      contains(["regional-mirror", "direct-source"], var.registry_delivery.mode) &&
      can(regex(
        "^(?:|[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?)$",
        var.registry_delivery.repository_prefix,
      )) &&
      alltrue([
        for host in var.registry_delivery.source_hosts :
        can(regex("^[a-zA-Z0-9.-]+(?::[0-9]+)?$", host))
      ])
    )
    error_message = "registry_delivery must select regional-mirror or direct-source, use a bounded repository prefix, and contain valid source registry hosts."
  }
}

variable "gpu_driver_preset" {
  description = "Deprecated B300 fixture guard. Generic GPU pools use their own provider.driver.preset from accelerator-pools.json."
  type        = string
  default     = "cuda13.0"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,31}$", var.gpu_driver_preset))
    error_message = "gpu_driver_preset must remain a bounded provider preset identifier; generic pools use their per-pool driver field."
  }
}

locals {
  approved_target_contract = jsondecode(file("${path.module}/../../catalog/profiles/approved-targets.json"))
  approved_targets         = local.approved_target_contract.targets
  catalog_target           = try(local.approved_targets[nonsensitive(var.project_id)], null)
  selected_target = {
    project_id = var.target_binding == null ? nonsensitive(var.project_id) : var.target_binding.project_id
    project_name = var.target_binding == null ? try(
      local.catalog_target.project_name,
      null,
    ) : var.target_binding.project_name
    region = var.target_binding == null ? try(
      local.catalog_target.region,
      null,
    ) : var.target_binding.region
    network_name = var.target_binding == null ? try(
      local.catalog_target.network_name,
      null,
    ) : var.target_binding.network_name
    subnet_name = var.target_binding == null ? try(
      local.catalog_target.subnet_name,
      null,
    ) : var.target_binding.subnet_name
    private_subnet_cidr = var.target_binding == null ? try(
      local.catalog_target.private_subnet_cidr,
      null,
    ) : var.target_binding.private_subnet_cidr
    system_update_strategy = var.target_binding == null ? try(
      local.catalog_target.system_update_strategy,
      null,
    ) : var.target_binding.system_update_strategy
  }

  capacity_profile_contract = jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json"))
  selected_capacity         = local.capacity_profile_contract.capacity_profiles[var.capacity_profile]

  effective_system_pool = {
    capacity = try(coalesce(var.system_pool.capacity, "regular"), "regular")
    platform = try(coalesce(var.system_pool.platform, "cpu-d3"), "cpu-d3")
    preset   = try(coalesce(var.system_pool.preset, "8vcpu-32gb"), "8vcpu-32gb")
    node_count = try(
      coalesce(var.system_pool.node_count, local.selected_capacity.system_nodes),
      local.selected_capacity.system_nodes,
    )
    boot_disk_type = try(coalesce(var.system_pool.boot_disk_type, "NETWORK_SSD"), "NETWORK_SSD")
    boot_disk_gib  = try(coalesce(var.system_pool.boot_disk_gib, 160), 160)
    max_surge = try(
      coalesce(var.system_pool.max_surge, local.selected_target.system_update_strategy.max_surge),
      local.selected_target.system_update_strategy.max_surge,
    )
    max_unavailable = try(
      coalesce(var.system_pool.max_unavailable, local.selected_target.system_update_strategy.max_unavailable),
      local.selected_target.system_update_strategy.max_unavailable,
    )
    drain_timeout = try(coalesce(var.system_pool.drain_timeout, "15m"), "15m")
  }

  effective_shared_cache = {
    size_gib = try(
      coalesce(var.shared_cache.size_gib, local.selected_capacity.shared_cache_size_gib),
      local.selected_capacity.shared_cache_size_gib,
    )
    type             = try(coalesce(var.shared_cache.type, "NETWORK_SSD"), "NETWORK_SSD")
    block_size_bytes = try(coalesce(var.shared_cache.block_size_bytes, 4096), 4096)
    forbid_deletion  = try(coalesce(var.shared_cache.forbid_deletion, false), false)
  }

  effective_reference_data = var.reference_data

  # Pool realization is separate from the capacity envelope. These two
  # aliases preserve the existing resource addresses and infrastructure
  # contract while moving provider/GPU facts behind the typed accelerator
  # contract. Profiles select an arbitrary map of independently qualified
  # pools; the legacy B300 fixture is retained separately for downstream v1
  # state custody while consumers migrate to the resolved v2 output.
  accelerator_pool_contract = jsondecode(file("${path.module}/../../catalog/profiles/accelerator-pools.json"))
  accelerator_pool_profile_contract = jsondecode(
    file("${path.module}/../../catalog/profiles/accelerator-pool-profiles.json")
  )
  effective_accelerator_pool_profile = coalesce(var.accelerator_pool_profile, var.capacity_profile)
  selected_accelerator_pool_profile  = local.accelerator_pool_profile_contract.profiles[local.effective_accelerator_pool_profile]
  using_custom_accelerator_pools     = length(var.custom_accelerator_pools) > 0
  accelerator_profile_supports_floor = local.using_custom_accelerator_pools || alltrue([
    for capacity in values(local.selected_accelerator_pool_profile.pools) :
    contains(keys(capacity.floor_nodes), var.gpu_floor_profile)
  ])
  profile_gpu_pools = {
    for pool_id, capacity in local.selected_accelerator_pool_profile.pools : pool_id => merge(
      local.accelerator_pool_contract.pool_templates[pool_id],
      {
        accelerator = local.accelerator_pool_contract.accelerator_classes[
          local.accelerator_pool_contract.pool_templates[pool_id].accelerator_class
        ]
        min_nodes = try(
          var.accelerator_pool_capacity_overrides[pool_id].min_nodes,
          try(capacity.floor_nodes[var.gpu_floor_profile], -1),
        )
        max_nodes = try(
          var.accelerator_pool_capacity_overrides[pool_id].max_nodes,
          capacity.max_nodes,
        )
        capacity_source = contains(keys(var.accelerator_pool_capacity_overrides), pool_id) ? "operator-override" : "profile"
        profile_bounds = {
          min_nodes = try(capacity.floor_nodes[var.gpu_floor_profile], -1)
          max_nodes = capacity.max_nodes
        }
        features = merge(
          local.accelerator_pool_contract.pool_templates[pool_id].features,
          { reference_data_filesystem = false },
        )
      },
    )
  }
  custom_gpu_pools = {
    for pool_id, pool in var.custom_accelerator_pools : pool_id => {
      id                = pool_id
      enabled           = true
      state             = "customer-specified"
      accelerator_class = pool.accelerator_class
      accelerator = {
        resource_api = {
          mode          = "extended-resource"
          resource_name = pool.resource_name
        }
      }
      provider = {
        name                   = "nebius"
        platform               = pool.platform
        preset                 = pool.preset
        node_group_name_suffix = substr(pool_id, 0, 32)
        node_group_label       = pool_id
        os                     = pool.os
        driver = {
          owner  = pool.driver.mode == "managed" ? "provider-managed" : "gpu-operator"
          preset = pool.driver.preset
        }
        reservation_policy = pool.reservation_policy == null ? "FORBID" : pool.reservation_policy.policy
        reservation_ids    = pool.reservation_policy == null ? [] : pool.reservation_policy.reservation_ids
      }
      node = {
        gpus_per_node         = pool.gpus_per_node
        gpu_memory_gb_nominal = pool.gpu_memory_gb
        vcpu_count            = null
        memory_gib            = null
        host_architectures    = [pool.host_architecture]
        topology              = pool.topology.mode
        boot_disk = {
          size_gib = pool.boot_disk.size_gib
          type     = pool.boot_disk.type
        }
        drain_timeout = pool.drain_timeout
      }
      capacity = {
        allowed_modes   = [pool.capacity_type]
        default_mode    = pool.capacity_type
        scale_from_zero = pool.topology.mode != "nvlink_rack" && pool.min_nodes == 0
      }
      scheduling = {
        stable_node_labels = merge({
          "workload.fs2.nebius/gpu"        = "true"
          "accelerator.fs2.nebius/class"   = pool.accelerator_class
          "accelerator.fs2.nebius/pool-id" = pool_id
          "capacity.fs2.nebius/type"       = pool.capacity_type
          "capacity.fs2.nebius/gpu-count"  = tostring(pool.gpus_per_node)
          "topology.fs2.nebius/scope"      = pool.topology.mode
          "local-nvme.fs2.nebius/eligible" = tostring(pool.local_nvme)
          "snapshot.fs2.nebius/eligible"   = tostring(pool.local_nvme)
          }, pool.reservation_policy == null ? {} : {
          "capacity.fs2.nebius/source" = "capacity-block"
          }
        )
        resource_flavor_name = "inference-${substr(pool_id, 0, 48)}"
        taints = [{
          key    = "dedicated"
          value  = "fs2-inference"
          effect = "NO_SCHEDULE"
        }]
        tolerations = [{
          key      = "dedicated"
          operator = "Equal"
          value    = "fs2-inference"
          effect   = "NoSchedule"
        }]
        forbidden_scale_zero_selectors = ["kubernetes.io/arch"]
      }
      features = {
        mig = {
          mode              = pool.mig.strategy
          resource_strategy = pool.mig.strategy == "none" ? "single" : pool.mig.strategy
          config            = pool.mig.config
        }
        local_storage = {
          mode            = pool.local_nvme ? "host-local-nvme" : "none"
          provider_config = pool.local_nvme ? (pool.local_nvme_mode == "raw" ? "passthrough-none" : "kubelet-ephemeral") : "none"
        }
        shared_filesystem         = pool.shared_filesystem
        reference_data_filesystem = pool.reference_data_filesystem
        local_cache               = pool.local_nvme ? "local-nvme" : (pool.shared_filesystem ? "shared-filesystem" : "none")
        gpu_snapshot              = pool.local_nvme ? "candidate-unvalidated" : "ineligible"
      }
      topology = pool.topology
      region_availability = [{
        region         = local.selected_target.region
        state          = "live-preflight-required"
        capacity_modes = [pool.capacity_type]
      }]
      evidence = {
        hardware_state = "live-preflight-required"
        reference      = null
      }
      min_nodes       = pool.min_nodes
      max_nodes       = pool.max_nodes
      capacity_source = "customer-tfvars"
      profile_bounds = {
        min_nodes = pool.min_nodes
        max_nodes = pool.max_nodes
      }
    }
  }
  selected_gpu_pools = merge(
    local.using_custom_accelerator_pools ? local.custom_gpu_pools : {},
    local.using_custom_accelerator_pools ? {} : local.profile_gpu_pools,
  )
  legacy_b300_pool_ids = toset([
    "nebius-b300-preemptible-1x",
    "nebius-b300-preemptible-8x",
  ])
  legacy_b300_fixture = (
    length(setsubtract(local.legacy_b300_pool_ids, toset(keys(local.selected_gpu_pools)))) == 0 &&
    length(setsubtract(toset(keys(local.selected_gpu_pools)), local.legacy_b300_pool_ids)) == 0 &&
    length(var.accelerator_pool_capacity_overrides) == 0 &&
    !local.using_custom_accelerator_pools &&
    local.effective_accelerator_pool_profile == var.capacity_profile &&
    var.target_binding == null &&
    var.system_pool == null &&
    var.shared_cache == null &&
    !var.reference_data.enabled
  )
  current_gpu_pool_1x = merge(
    local.accelerator_pool_contract.pool_templates["nebius-b300-preemptible-1x"],
    {
      min_nodes = try(local.selected_gpu_pools["nebius-b300-preemptible-1x"].min_nodes, 0)
      max_nodes = try(local.selected_gpu_pools["nebius-b300-preemptible-1x"].max_nodes, 0)
    },
  )
  current_gpu_pool_8x = merge(
    local.accelerator_pool_contract.pool_templates["nebius-b300-preemptible-8x"],
    {
      min_nodes = try(local.selected_gpu_pools["nebius-b300-preemptible-8x"].min_nodes, 0)
      max_nodes = try(local.selected_gpu_pools["nebius-b300-preemptible-8x"].max_nodes, 0)
    },
  )

  resolved_accelerator_pool_contract = {
    schema        = "fs2-serve.nebius.ai/terraform-accelerator-pools/v2"
    source_commit = var.source_commit
    profile       = local.using_custom_accelerator_pools ? "custom" : local.effective_accelerator_pool_profile
    floor_profile = var.gpu_floor_profile
    target_region = local.selected_target.region
    capacity_ownership = {
      owner_root                 = "infra-disposable"
      override_mode              = "capacity-only-patch"
      override_fields            = ["max_nodes", "min_nodes"]
      requested_overrides        = var.accelerator_pool_capacity_overrides
      requested_overrides_sha256 = sha256(jsonencode(var.accelerator_pool_capacity_overrides))
    }
    artifact_source = {
      deprecated = true
      registry = {
        id           = nebius_registry_v1_registry.images.id
        project_id   = nonsensitive(var.project_id)
        project_name = data.nebius_iam_v2_project.target.name
        region       = local.selected_target.region
        fqdn         = nebius_registry_v1_registry.images.status.registry_fqdn
      }
      closure_schema = jsondecode(file("${path.module}/../../catalog/profiles/source-registry-closure.json")).schema
      closure_sha256 = filesha256("${path.module}/../../catalog/profiles/source-registry-closure.json")
      cross_region_pull_required = (
        var.registry_delivery.mode == "direct-source" &&
        length(local.cross_region_source_hosts) > 0
      )
    }
    artifact_delivery = {
      mode                  = var.registry_delivery.mode
      repository_prefix     = var.registry_delivery.repository_prefix
      upstream_registry_ids = sort(tolist(var.external_registry_ids))
      source_hosts          = var.registry_delivery.source_hosts
      target_registry = {
        id           = nebius_registry_v1_registry.images.id
        project_id   = nonsensitive(var.project_id)
        project_name = data.nebius_iam_v2_project.target.name
        region       = local.selected_target.region
        fqdn         = nebius_registry_v1_registry.images.status.registry_fqdn
      }
      promotion_cross_region_required = (
        var.registry_delivery.mode == "regional-mirror" &&
        length(local.cross_region_source_hosts) > 0
      )
      runtime_cross_region_pull_required = (
        var.registry_delivery.mode == "direct-source" &&
        length(local.cross_region_source_hosts) > 0
      )
    }
    pools = {
      for pool_id, pool in local.selected_gpu_pools : pool_id => {
        id                = pool.id
        accelerator_class = pool.accelerator_class
        resource_api      = pool.accelerator.resource_api
        provider          = pool.provider
        node              = pool.node
        capacity = {
          type            = pool.capacity.default_mode
          min_nodes       = pool.min_nodes
          max_nodes       = pool.max_nodes
          source          = pool.capacity_source
          profile_bounds  = pool.profile_bounds
          scale_from_zero = pool.capacity.scale_from_zero
        }
        scheduling          = pool.scheduling
        features            = pool.features
        region_availability = pool.region_availability
        state               = pool.state
        evidence            = pool.evidence
      }
    }
  }

  legacy_infrastructure_contract = {
    schema        = "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1"
    source_commit = var.source_commit
    target = {
      project_id = nonsensitive(var.project_id)
      region     = local.selected_target.region
      system_update_strategy = {
        max_surge       = local.effective_system_pool.max_surge
        max_unavailable = local.effective_system_pool.max_unavailable
      }
    }
    source_registry = {
      id         = nebius_registry_v1_registry.images.id
      project_id = nonsensitive(var.project_id)
      fqdn       = nebius_registry_v1_registry.images.status.registry_fqdn
    }
    capacity = {
      profile               = var.capacity_profile
      floor_profile         = var.gpu_floor_profile
      maximum_gpus          = local.selected_capacity.maximum_gpus
      shared_cache_size_gib = local.effective_shared_cache.size_gib
      system = {
        capacity        = local.effective_system_pool.capacity
        platform        = local.effective_system_pool.platform
        preset          = local.effective_system_pool.preset
        nodes           = local.effective_system_pool.node_count
        max_surge       = local.effective_system_pool.max_surge
        max_unavailable = local.effective_system_pool.max_unavailable
      }
      gpu_b300_1x = {
        capacity      = local.current_gpu_pool_1x.capacity.default_mode
        platform      = local.current_gpu_pool_1x.provider.platform
        preset        = local.current_gpu_pool_1x.provider.preset
        gpus_per_node = local.current_gpu_pool_1x.node.gpus_per_node
        min_nodes     = local.current_gpu_pool_1x.min_nodes
        max_nodes     = local.current_gpu_pool_1x.max_nodes
        driver_preset = local.current_gpu_pool_1x.provider.driver.preset
        local_nvme    = local.current_gpu_pool_1x.features.local_cache == "local-nvme"
      }
      gpu_b300_8x = {
        capacity      = local.current_gpu_pool_8x.capacity.default_mode
        platform      = local.current_gpu_pool_8x.provider.platform
        preset        = local.current_gpu_pool_8x.provider.preset
        gpus_per_node = local.current_gpu_pool_8x.node.gpus_per_node
        min_nodes     = local.current_gpu_pool_8x.min_nodes
        max_nodes     = local.current_gpu_pool_8x.max_nodes
        driver_preset = local.current_gpu_pool_8x.provider.driver.preset
        local_nvme    = local.current_gpu_pool_8x.features.local_cache == "local-nvme"
      }
    }
  }
  infrastructure_contract = local.legacy_b300_fixture ? local.legacy_infrastructure_contract : null
}
