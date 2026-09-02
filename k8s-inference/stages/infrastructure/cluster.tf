locals {
  shared_cache_mount_path           = "/mnt/fs2cache"
  reference_data_mount_path         = "/mnt/fs2-reference-data"
  reference_data_host_path          = "${local.reference_data_mount_path}/data"
  shared_cache_cloud_init_user_data = <<-YAML
    #cloud-config
    package_update: false
    package_upgrade: false
    mounts:
      - [fs2cache, ${local.shared_cache_mount_path}, virtiofs, "defaults,nofail", 0, 2]
${var.reference_data.enabled ? format("      - [fs2reference, %s, virtiofs, \"defaults,nofail\", 0, 2]", local.reference_data_mount_path) : ""}
    runcmd:
      - [modprobe, fuse]
      - [mkdir, -p, ${local.shared_cache_mount_path}]
${var.reference_data.enabled ? format("      - [mkdir, -p, %s]", local.reference_data_mount_path) : ""}
      - [mount, -a]
      - [mkdir, -p, ${local.shared_cache_mount_path}/csi-mounted-fs-path-data]
${var.reference_data.enabled ? format("      - [install, -d, -m, \"0770\", -o, \"1000\", -g, \"1000\", %s]", local.reference_data_host_path) : ""}
  YAML
  filesystem_attachment = concat(
    [{
      attach_mode = "READ_WRITE"
      mount_tag   = "fs2cache"
      existing_filesystem = {
        id = nebius_compute_v1_filesystem.cache.id
      }
    }],
    var.reference_data.enabled ? [{
      attach_mode = "READ_WRITE"
      mount_tag   = "fs2reference"
      existing_filesystem = {
        id = nebius_compute_v1_filesystem.reference_data[0].id
      }
    }] : [],
  )

  worker_network_interfaces = [{
    subnet_id = data.nebius_vpc_v1_subnet.target.id
    security_groups = [{
      id = nebius_vpc_v1_security_group.workers.id
    }]
  }]

  gpu_local_disks = {
    "passthrough-none" = {
      passthrough_group = {
        requested = true
      }
      config = {
        none = true
      }
    }
    "kubelet-ephemeral" = {
      passthrough_group = {
        requested = true
      }
      config = {
        kubelet_ephemeral = true
      }
    }
  }

  standard_gpu_pools = {
    for pool_id, pool in local.selected_gpu_pools : pool_id => pool
    if pool.node.topology != "nvlink_rack"
  }
  gpu_cluster_pools = {
    for pool_id, pool in local.selected_gpu_pools : pool_id => pool
    if try(length(trimspace(pool.topology.infiniband_fabric)) > 0, false)
  }
  nvlink_racks = merge({}, [
    for pool_id, pool in local.selected_gpu_pools : {
      for rack_index in range(try(pool.topology.rack_count, 0)) :
      format("%s-rack-%03d", pool_id, rack_index + 1) => {
        pool_id    = pool_id
        rack_index = rack_index + 1
        pool       = pool
      }
    } if pool.node.topology == "nvlink_rack"
  ]...)
}

resource "nebius_compute_v1_gpu_cluster" "pool" {
  for_each = local.gpu_cluster_pools

  parent_id         = var.project_id
  name              = "${local.resource_name}-${substr(each.key, 0, 24)}-fabric"
  infiniband_fabric = each.value.topology.infiniband_fabric
}

resource "nebius_mk8s_v1_cluster" "validation" {
  parent_id = var.project_id
  name      = local.resource_name
  labels    = merge(local.common_labels, { purpose = "validation-cluster" })

  control_plane = {
    subnet_id         = data.nebius_vpc_v1_subnet.target.id
    version           = var.kubernetes_version
    etcd_cluster_size = 3
    audit_logs        = {}
    endpoints = {
      public_endpoint = {
        allowed_cidrs = var.control_plane_allowed_cidrs
      }
    }
  }

  depends_on = [
    terraform_data.target_contract,
    nebius_vpc_v1_security_rule.workers_private_ingress,
    nebius_vpc_v1_security_rule.workers_egress,
  ]
}

resource "nebius_mk8s_v1_node_group" "system" {
  parent_id        = nebius_mk8s_v1_cluster.validation.id
  name             = "${local.resource_name}-system"
  labels           = merge(local.common_labels, { pool = "system" })
  version          = var.kubernetes_version
  fixed_node_count = local.effective_system_pool.node_count

  strategy = {
    max_surge       = { count = local.effective_system_pool.max_surge }
    max_unavailable = { count = local.effective_system_pool.max_unavailable }
    drain_timeout   = local.effective_system_pool.drain_timeout
  }

  template = {
    metadata = {
      labels = {
        "workload.fs2.nebius/system"        = "true"
        "capacity.fs2.nebius/type"          = local.effective_system_pool.capacity
        "capacity.fs2.nebius/pool"          = "system"
        "lifecycle.fs2.nebius/run"          = var.run_id
        "storage.fs2.nebius/shared-cache"   = "true"
        "storage.fs2.nebius/reference-data" = var.reference_data.enabled ? "true" : "false"
      }
    }
    boot_disk = {
      size_gibibytes = local.effective_system_pool.boot_disk_gib
      type           = local.effective_system_pool.boot_disk_type
    }
    filesystems        = local.filesystem_attachment
    network_interfaces = local.worker_network_interfaces
    os                 = "ubuntu24.04"
    reservation_policy = { policy = "FORBID" }
    resources = {
      platform = local.effective_system_pool.platform
      preset   = local.effective_system_pool.preset
    }
    service_account_id   = nebius_iam_v1_service_account.nodepull.id
    underlay_required    = false
    cloud_init_user_data = local.shared_cache_cloud_init_user_data
  }

  depends_on = [
    nebius_iam_v1_group_membership.nodepull_target_registry,
    nebius_iam_v1_group_membership.nodepull_external_registry,
    nebius_iam_v1_access_permit.nodepull_registry,
    nebius_iam_v1_access_permit.nodepull_external_registry,
    nebius_compute_v1_filesystem.cache,
    nebius_compute_v1_filesystem.reference_data,
  ]
}

moved {
  from = nebius_mk8s_v1_node_group.gpu_b300_1x
  to   = nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-1x"]
}

moved {
  from = nebius_mk8s_v1_node_group.gpu_b300_8x
  to   = nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-8x"]
}

resource "nebius_mk8s_v1_node_group" "gpu" {
  for_each = local.standard_gpu_pools

  parent_id = nebius_mk8s_v1_cluster.validation.id
  name      = "${local.resource_name}-${each.value.provider.node_group_name_suffix}"
  labels    = merge(local.common_labels, { pool = each.value.provider.node_group_label })
  version   = var.kubernetes_version

  fixed_node_count = each.value.provider.reservation_policy != "FORBID" ? each.value.max_nodes : null

  autoscaling = each.value.provider.reservation_policy == "FORBID" ? {
    min_node_count = each.value.min_nodes
    max_node_count = each.value.max_nodes
  } : null

  strategy = {
    max_surge       = { count = each.value.provider.reservation_policy == "FORBID" ? 1 : 0 }
    max_unavailable = { count = each.value.provider.reservation_policy == "FORBID" ? 0 : 1 }
    drain_timeout   = each.value.node.drain_timeout
  }

  template = {
    metadata = {
      labels = merge(
        each.value.scheduling.stable_node_labels,
        each.value.features.mig.mode == "none" ? {} : {
          "nvidia.com/mig.config" = each.value.features.mig.config
        },
        each.value.features.shared_filesystem ? {
          "storage.fs2.nebius/shared-cache" = "true"
        } : {},
        each.value.features.shared_filesystem && var.reference_data.enabled ? {
          "storage.fs2.nebius/reference-data" = "true"
        } : {},
        {
          "lifecycle.fs2.nebius/run" = var.run_id
        },
      )
    }
    taints = each.value.scheduling.taints
    boot_disk = {
      size_gibibytes = each.value.node.boot_disk.size_gib
      type           = each.value.node.boot_disk.type
    }
    filesystems = each.value.features.shared_filesystem ? local.filesystem_attachment : []
    gpu_settings = each.value.provider.driver.owner == "provider-managed" ? {
      drivers_preset = each.value.provider.driver.preset
    } : null
    local_disks        = each.value.features.local_storage.mode == "host-local-nvme" ? local.gpu_local_disks[each.value.features.local_storage.provider_config] : null
    network_interfaces = local.worker_network_interfaces
    os                 = each.value.provider.os
    preemptible        = each.value.capacity.default_mode == "preemptible" ? {} : null
    reservation_policy = {
      policy          = each.value.provider.reservation_policy
      reservation_ids = length(try(each.value.provider.reservation_ids, [])) > 0 ? each.value.provider.reservation_ids : null
    }
    service_account_id   = nebius_iam_v1_service_account.nodepull.id
    underlay_required    = false
    cloud_init_user_data = each.value.features.shared_filesystem ? local.shared_cache_cloud_init_user_data : null
    resources = {
      platform = each.value.provider.platform
      preset   = each.value.provider.preset
    }
    gpu_cluster = each.value.node.topology == "gpu_cluster" ? nebius_compute_v1_gpu_cluster.pool[each.key] : null
  }

  depends_on = [
    module.device_plugin,
    module.gpu_operator,
    module.network_operator,
  ]

  lifecycle {
    precondition {
      condition     = local.accelerator_profile_supports_floor
      error_message = "gpu_floor_profile is not present in every pool of the selected accelerator_pool_profile."
    }

    precondition {
      condition = local.using_custom_accelerator_pools ? (
        each.value.id == each.key &&
        each.value.provider.name == "nebius" &&
        each.value.min_nodes >= 0 &&
        each.value.min_nodes <= each.value.max_nodes &&
        contains(["provider-managed", "gpu-operator"], each.value.provider.driver.owner) &&
        (each.value.features.mig.mode == "none" || each.value.provider.driver.owner == "gpu-operator") &&
        (each.value.provider.reservation_policy == "FORBID" ? (
          length(try(each.value.provider.reservation_ids, [])) == 0
          ) : (
          contains(["AUTO", "STRICT"], each.value.provider.reservation_policy) &&
          each.value.capacity.default_mode == "regular" &&
          each.value.min_nodes >= 1 &&
          each.value.min_nodes == each.value.max_nodes
        )) &&
        each.value.node.topology != "nvlink_rack"
        ) : (
        each.value.id == each.key &&
        each.value.enabled &&
        each.value.state == "hardware-validated" &&
        each.value.evidence.hardware_state == "hardware-validated" &&
        each.value.provider.name == "nebius" &&
        each.value.provider.driver.owner == "provider-managed" &&
        each.value.accelerator.resource_api.mode == "extended-resource" &&
        each.value.accelerator.resource_api.resource_name == "nvidia.com/gpu" &&
        each.value.features.mig.mode == "disabled" &&
        each.value.min_nodes >= 0 &&
        each.value.min_nodes <= each.value.max_nodes &&
        contains(each.value.capacity.allowed_modes, each.value.capacity.default_mode) &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/pool-id"] == each.key &&
        each.value.scheduling.stable_node_labels["accelerator.fs2.nebius/class"] == each.value.accelerator_class &&
        length([
          for availability in each.value.region_availability : availability
          if availability.region == local.selected_target.region &&
          availability.state == "hardware-validated" &&
          contains(availability.capacity_modes, each.value.capacity.default_mode)
        ]) == 1
      )
      error_message = "accelerator pool ${each.key} is not an enabled, hardware-validated Nebius extended-resource realization for the exact target region and capacity mode."
    }

    precondition {
      condition     = !local.legacy_b300_fixture || var.gpu_driver_preset == each.value.provider.driver.preset
      error_message = "the deprecated gpu_driver_preset input must match every selected pool's provider-managed driver preset."
    }
  }
}

resource "nebius_compute_v1_nvl_instance_group" "rack" {
  for_each = local.nvlink_racks

  parent_id = var.project_id
  name      = "${local.resource_name}-${substr(each.key, 0, 30)}"
  size      = each.value.pool.topology.nodes_per_rack
  type      = "GB300"
}

resource "nebius_mk8s_v1_node_group" "nvlink_rack" {
  for_each = local.nvlink_racks

  parent_id        = nebius_mk8s_v1_cluster.validation.id
  name             = "${local.resource_name}-${substr(each.key, 0, 30)}"
  version          = var.kubernetes_version
  fixed_node_count = each.value.pool.topology.nodes_per_rack

  labels = merge(local.common_labels, {
    pool                               = each.value.pool.provider.node_group_label
    "nebius.com/nvlink-instance-group" = nebius_compute_v1_nvl_instance_group.rack[each.key].id
  })

  strategy = {
    max_surge       = { count = 0 }
    max_unavailable = { count = 1 }
    drain_timeout   = each.value.pool.node.drain_timeout
  }

  template = {
    metadata = {
      labels = merge(
        each.value.pool.scheduling.stable_node_labels,
        each.value.pool.features.shared_filesystem ? {
          "storage.fs2.nebius/shared-cache" = "true"
        } : {},
        each.value.pool.features.shared_filesystem && var.reference_data.enabled ? {
          "storage.fs2.nebius/reference-data" = "true"
        } : {},
        {
          "lifecycle.fs2.nebius/run"         = var.run_id
          "nebius.com/nvlink-instance-group" = nebius_compute_v1_nvl_instance_group.rack[each.key].id
        },
      )
    }
    boot_disk = {
      size_gibibytes = each.value.pool.node.boot_disk.size_gib
      type           = each.value.pool.node.boot_disk.type
    }
    taints      = each.value.pool.scheduling.taints
    filesystems = each.value.pool.features.shared_filesystem ? local.filesystem_attachment : []
    gpu_settings = {
      drivers_preset = each.value.pool.provider.driver.preset
    }
    local_disks        = each.value.pool.features.local_storage.mode == "host-local-nvme" ? local.gpu_local_disks[each.value.pool.features.local_storage.provider_config] : null
    network_interfaces = local.worker_network_interfaces
    nvlink = {
      nvl_instance_group_id = nebius_compute_v1_nvl_instance_group.rack[each.key].id
    }
    gpu_cluster = try(length(trimspace(each.value.pool.topology.infiniband_fabric)) > 0, false) ? nebius_compute_v1_gpu_cluster.pool[each.value.pool_id] : null
    os          = each.value.pool.provider.os
    reservation_policy = {
      policy          = each.value.pool.provider.reservation_policy
      reservation_ids = length(try(each.value.pool.provider.reservation_ids, [])) > 0 ? each.value.pool.provider.reservation_ids : null
    }
    service_account_id   = nebius_iam_v1_service_account.nodepull.id
    underlay_required    = false
    cloud_init_user_data = each.value.pool.features.shared_filesystem ? local.shared_cache_cloud_init_user_data : null
    resources = {
      platform = each.value.pool.provider.platform
      preset   = each.value.pool.provider.preset
    }
  }

  lifecycle {
    precondition {
      condition = (
        local.using_custom_accelerator_pools &&
        each.value.pool.capacity.default_mode == "regular" &&
        each.value.pool.provider.driver.owner == "provider-managed" &&
        each.value.pool.features.mig.mode == "none" &&
        each.value.pool.provider.platform == "gpu-gb300" &&
        each.value.pool.provider.preset == "4gpu-112vcpu-800gb" &&
        each.value.pool.accelerator_class == "nvidia-gb300" &&
        each.value.pool.node.gpus_per_node == 4 &&
        each.value.pool.node.host_architectures == ["arm64"] &&
        each.value.pool.topology.nodes_per_rack == 18 &&
        (each.value.pool.topology.rack_count == 1 || try(length(trimspace(each.value.pool.topology.infiniband_fabric)) > 0, false)) &&
        each.value.pool.min_nodes == each.value.pool.max_nodes &&
        each.value.pool.max_nodes == each.value.pool.topology.rack_count * each.value.pool.topology.nodes_per_rack
      )
      error_message = "NVLink rack pools require fixed regular 18-node GB300/ARM64 capacity, managed drivers, MIG disabled, and an InfiniBand fabric when multiple racks are requested."
    }
  }

  depends_on = [
    module.device_plugin,
    module.gpu_operator,
    module.network_operator,
  ]
}
