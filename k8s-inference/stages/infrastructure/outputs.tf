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
  description = "Non-secret reviewed target and source-registry identity used by downstream acceptance receipts."
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

output "node_group_ids" {
  description = "Backward-compatible node-group map. The two B300 aliases are present only for the exact legacy fixture."
  value = merge(
    { system = nebius_mk8s_v1_node_group.system.id },
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
    registry            = nebius_registry_v1_registry.images.id
    shared_cache        = nebius_compute_v1_filesystem.cache.id
    nodepull_sa         = nebius_iam_v1_service_account.nodepull.id
    target_reader_group = nebius_iam_v1_group.target_registry_readers.id
    worker_sg           = nebius_vpc_v1_security_group.workers.id
    gateway_allocation  = try(one(nebius_vpc_v1_allocation.gateway[*].id), null)
  }
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
      application_origin       = var.public_edge_mode == "internal-only" ? "http://localhost:18082" : null
      operator_endpoint        = var.public_edge_mode == "internal-only" ? "http://127.0.0.1:18082" : null
      operator_proxy_port      = var.public_edge_mode == "internal-only" ? 18082 : null
      control_plane_service    = "fs2-serve-control-plane"
      control_plane_port       = 8080
      control_plane_local_port = var.public_edge_mode == "internal-only" ? 18080 : null
      admin_console_service    = "fs2-serve-control-plane-admin-console"
      admin_console_port       = 8080
      admin_console_local_port = var.public_edge_mode == "internal-only" ? 18081 : null
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
