output "cluster_id" {
  description = "Disposable Managed Kubernetes cluster ID."
  value       = nebius_mk8s_v1_cluster.validation.id
}

output "cluster_name" {
  value = nebius_mk8s_v1_cluster.validation.name
}

output "cluster_version" {
  value = nebius_mk8s_v1_cluster.validation.status.control_plane.version
}

output "target_contract" {
  description = "Non-secret reviewed target and legacy source_registry alias used by downstream acceptance receipts. New consumers should use registry_delivery_contract; source_registry names the cluster-local target registry for compatibility."
  value = {
    project_id                 = nonsensitive(var.project_id)
    project_name               = coalesce(local.selected_target.project_name, data.nebius_iam_v2_project.target.name)
    region                     = local.selected_target.region
    network_name               = local.selected_target.network_name
    subnet_name                = local.selected_target.subnet_name
    private_subnet_cidr        = local.selected_target.private_subnet_cidr
    source_registry_project_id = nonsensitive(var.project_id)
    system_update_strategy = {
      max_surge       = local.effective_system_pool.max_surge
      max_unavailable = local.effective_system_pool.max_unavailable
    }
    tenant_id = data.nebius_iam_v2_project.target.parent_id
    source_registry = {
      id         = nebius_registry_v1_registry.images.id
      project_id = nonsensitive(var.project_id)
      fqdn       = nebius_registry_v1_registry.images.status.registry_fqdn
    }
  }
}

output "registry_delivery_contract" {
  description = "Canonical non-secret distinction between upstream image sources, one Terraform-created regional target registry, promotion traffic, and node runtime pulls."
  value = {
    schema            = "fs2-serve.nebius.ai/registry-delivery/v1"
    mode              = var.registry_delivery.mode
    repository_prefix = var.registry_delivery.repository_prefix
    target_registry = {
      id              = nebius_registry_v1_registry.images.id
      project_id      = nonsensitive(var.project_id)
      region          = local.selected_target.region
      fqdn            = nebius_registry_v1_registry.images.status.registry_fqdn
      repository_root = "${nebius_registry_v1_registry.images.status.registry_fqdn}/${trimprefix(nebius_registry_v1_registry.images.id, "registry-")}"
    }
    upstream = {
      source_hosts = var.registry_delivery.source_hosts
      nebius_registries = {
        for registry_id, registry in data.nebius_registry_v1_registry.external : registry_id => {
          id         = registry.id
          project_id = registry.parent_id
          fqdn       = registry.status.registry_fqdn
          region     = try(regex("^cr\\.([a-z0-9-]+)\\.nebius\\.cloud$", registry.status.registry_fqdn)[0], null)
        }
      }
    }
    promotion_cross_region_required = (
      var.registry_delivery.mode == "regional-mirror" &&
      length(local.cross_region_source_hosts) > 0
    )
    runtime_cross_region_pull_required = (
      var.registry_delivery.mode == "direct-source" &&
      length(local.cross_region_source_hosts) > 0
    )
    cross_region_source_hosts = local.cross_region_source_hosts
  }
}

output "node_group_ids" {
  description = "Backward-compatible node-group map. The two B300 aliases are present only for the exact legacy fixture."
  value = merge(
    { system = nebius_mk8s_v1_node_group.system.id },
    var.reference_data.enabled ? {
      reference_data_cpu = nebius_mk8s_v1_node_group.reference_data[0].id
    } : {},
    contains(keys(nebius_mk8s_v1_node_group.gpu), "nebius-b300-preemptible-1x") ? {
      gpu_b300_1x = nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-1x"].id
    } : {},
    contains(keys(nebius_mk8s_v1_node_group.gpu), "nebius-b300-preemptible-8x") ? {
      gpu_b300_8x = nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-8x"].id
    } : {},
  )
}

output "accelerator_node_group_ids" {
  description = "Stable pool or rack ID to Nebius node-group ID map for heterogeneous consumers."
  value = merge(
    { for pool_id, node_group in nebius_mk8s_v1_node_group.gpu : pool_id => node_group.id },
    { for rack_id, node_group in nebius_mk8s_v1_node_group.nvlink_rack : rack_id => node_group.id },
  )
}

output "accelerator_pool_contract" {
  description = "Resolved non-secret heterogeneous pool contract, including effective profile or capacity-only override bounds."
  value       = local.resolved_accelerator_pool_contract
}

output "accelerator_pool_contract_sha256" {
  description = "Canonical digest that later Terraform stages must match before reporting accelerator pool capacity as effective."
  value       = sha256(jsonencode(local.resolved_accelerator_pool_contract))
}

output "owned_resource_ids" {
  description = "Task-owned resources that must be absent after the supervised destroy."
  value = {
    registry                  = nebius_registry_v1_registry.images.id
    shared_cache              = nebius_compute_v1_filesystem.cache.id
    reference_data_filesystem = try(nebius_compute_v1_filesystem.reference_data[0].id, null)
    reference_data_bucket     = try(nebius_storage_v1_bucket.reference_data[0].id, null)
    reference_data_writer_sa  = try(nebius_iam_v1_service_account.reference_data[0].id, null)
    reference_data_access_key = try(nebius_iam_v2_access_key.reference_data[0].id, null)
    reference_data_cpu_pool   = try(nebius_mk8s_v1_node_group.reference_data[0].id, null)
    nodepull_sa               = nebius_iam_v1_service_account.nodepull.id
    target_reader_group       = nebius_iam_v1_group.target_registry_readers.id
    external_reader_groups = {
      for registry_id, group in nebius_iam_v1_group.external_registry_readers : registry_id => group.id
    }
    worker_sg          = nebius_vpc_v1_security_group.workers.id
    gateway_allocation = try(one(nebius_vpc_v1_allocation.gateway[*].id), null)
  }
}

output "reference_data_storage_contract" {
  description = "Dedicated same-region durable filesystem/object contract for immutable scientific reference data, or null when disabled."
  value = var.reference_data.enabled ? {
    schema     = "fs2-serve.nebius.ai/reference-data-storage/v1"
    project_id = nonsensitive(var.project_id)
    region     = local.selected_target.region
    cpu_pool = {
      id         = nebius_mk8s_v1_node_group.reference_data[0].id
      name       = nebius_mk8s_v1_node_group.reference_data[0].name
      platform   = var.reference_data.cpu_pool.platform
      preset     = var.reference_data.cpu_pool.preset
      node_count = var.reference_data.cpu_pool.node_count
      capacity   = "regular"
      node_labels = {
        "workload.fs2.nebius/reference-data" = "true"
        "capacity.fs2.nebius/type"           = "regular"
        "capacity.fs2.nebius/pool"           = "reference-data"
        "storage.fs2.nebius/reference-data"  = "true"
      }
      taint = {
        key    = "workload.fs2.nebius/reference-data"
        value  = "true"
        effect = "NoSchedule"
      }
    }
    filesystem = {
      id               = nebius_compute_v1_filesystem.reference_data[0].id
      size_gib         = var.reference_data.filesystem.size_gib
      type             = var.reference_data.filesystem.type
      block_size_bytes = var.reference_data.filesystem.block_size_bytes
      forbid_deletion  = var.reference_data.filesystem.forbid_deletion
      node_mount_path  = local.reference_data_mount_path
      host_path        = local.reference_data_host_path
      uri              = "file://${local.reference_data_host_path}"
    }
    object_storage = {
      id                = nebius_storage_v1_bucket.reference_data[0].id
      name              = nebius_storage_v1_bucket.reference_data[0].name
      endpoint          = "https://storage.${local.selected_target.region}.nebius.cloud"
      max_size_gib      = var.reference_data.object_storage.max_size_gib
      versioning_policy = "ENABLED"
      object_prefix     = "s3://${nebius_storage_v1_bucket.reference_data[0].name}/reference-data"
    }
    layout = {
      blobs                 = "s3://${nebius_storage_v1_bucket.reference_data[0].name}/reference-data/blobs/sha256/<sha256>"
      manifests             = "s3://${nebius_storage_v1_bucket.reference_data[0].name}/reference-data/manifests/sha256/<manifest-sha256>.json"
      filesystem_datasets   = "file://${local.reference_data_host_path}/datasets/<bundle>/<revision>/sha256/<tree-sha256>"
      preprocessing_inputs  = "s3://${nebius_storage_v1_bucket.reference_data[0].name}/inputs/sha256/<sha256>.<format>"
      preprocessing_outputs = "s3://${nebius_storage_v1_bucket.reference_data[0].name}/preprocessing/<tenant>/<workload>/requests/sha256/<request-sha256>/results/sha256/<result-manifest-sha256>"
    }
    sizing = {
      official_alphafold3_expanded_bytes = 630000000000
      required_headroom_bytes            = 1099511627776
      minimum_size_gib                   = 1611
    }
    public_msa_default = false
  } : null
}

output "reference_data_object_storage_access" {
  description = "Sensitive handoff containing only the S3 access-key ID and MysteryBox reference; the secret value never enters infrastructure state or generated tfvars."
  sensitive   = true
  value = var.reference_data.enabled ? {
    access_key_id       = nebius_iam_v2_access_key.reference_data[0].status.aws_access_key_id
    secret_reference_id = nebius_iam_v2_access_key.reference_data[0].status.secret_reference_id
    revision            = nebius_iam_v2_access_key.reference_data[0].resource_version
  } : null
}

output "gateway_allocation_id" {
  description = "Run-owned public allocation ID, or null in internal-only mode."
  value       = try(one(nebius_vpc_v1_allocation.gateway[*].id), null)
}

output "gateway_public_cidr" {
  description = "Run-owned public /32, or null in internal-only mode."
  value       = try(one(nebius_vpc_v1_allocation.gateway[*].status.details.allocated_cidr), null)
}

output "public_edge_contract" {
  description = "Typed handoff for the workload edge. Internal-only mode contains no placeholder allocation identity."
  value = {
    schema                  = "fs2-serve.nebius.ai/public-edge/v1"
    mode                    = var.public_edge_mode
    transport               = var.public_edge_mode == "public" ? "public-https" : "kubectl-port-forward"
    public_origin           = var.public_edge_mode == "public" ? try("https://${split("/", one(nebius_vpc_v1_allocation.gateway[*].status.details.allocated_cidr))[0]}", null) : null
    allocation_project_id   = var.public_edge_mode == "public" ? nonsensitive(var.project_id) : null
    allocation_id           = try(one(nebius_vpc_v1_allocation.gateway[*].id), null)
    public_ipv4_address     = try(split("/", one(nebius_vpc_v1_allocation.gateway[*].status.details.allocated_cidr))[0], null)
    external_traffic_policy = "Cluster"
    service_ports           = var.public_edge_service_ports
    port_forward = {
      enabled                  = var.public_edge_mode == "internal-only"
      bind_address             = var.public_edge_mode == "internal-only" ? "127.0.0.1" : null
      application_origin       = var.public_edge_mode == "internal-only" ? format("http://localhost:%d", var.port_forward_local_ports.operator_proxy) : null
      operator_endpoint        = var.public_edge_mode == "internal-only" ? format("http://127.0.0.1:%d", var.port_forward_local_ports.operator_proxy) : null
      operator_proxy_port      = var.public_edge_mode == "internal-only" ? var.port_forward_local_ports.operator_proxy : null
      control_plane_service    = "fs2-serve-control-plane"
      control_plane_port       = 8080
      control_plane_local_port = var.public_edge_mode == "internal-only" ? var.port_forward_local_ports.control_plane : null
      admin_console_service    = "fs2-serve-control-plane-admin-console"
      admin_console_port       = 8080
      admin_console_local_port = var.public_edge_mode == "internal-only" ? var.port_forward_local_ports.admin_console : null
    }
    security_group_destination_ports = var.public_edge_mode == "public" ? [
      var.public_edge_service_ports.http.listener_port,
      var.public_edge_service_ports.https.listener_port,
      var.public_edge_service_ports.http.target_port,
      var.public_edge_service_ports.https.target_port,
      var.public_edge_service_ports.http.node_port,
      var.public_edge_service_ports.https.node_port,
    ] : []
  }
}

output "capacity_contract" {
  description = "Capacity-only compatibility view of the cross-stage infrastructure contract."
  value       = try(local.infrastructure_contract.capacity, null)
}

output "infrastructure_contract" {
  description = "Legacy v1 B300 contract passed unchanged only when no capacity override is active; otherwise null forces v2-aware consumers."
  value       = local.infrastructure_contract
}

output "kubeconfig_command" {
  description = "Run only with KUBECONFIG pointing at the run-scoped mode-0600 file."
  value       = "nebius mk8s cluster get-credentials --id ${nebius_mk8s_v1_cluster.validation.id} --external --force --context-name ${local.resource_name}"
}
