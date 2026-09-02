data "terraform_remote_state" "foundation" {
  backend = "local"
  config = {
    path = local.expected_foundation_state
  }
}

data "kubernetes_namespace_v1" "kube_system" {
  metadata { name = "kube-system" }
}

data "kubernetes_service_v1" "kubernetes_api" {
  metadata {
    name      = "kubernetes"
    namespace = "default"
  }
}

# Cilium enforces this egress after Service DNAT on Nebius Managed Kubernetes.
# Discover the ready control-plane endpoints instead of assuming the Service
# ClusterIP remains the packet destination. Exact ready endpoint routes aid
# diagnosis; the target-contract private subnet is the stable rotation fallback.
data "kubernetes_resources" "kubernetes_api_endpoint_slices" {
  api_version    = "discovery.k8s.io/v1"
  kind           = "EndpointSlice"
  namespace      = "default"
  label_selector = "kubernetes.io/service-name=kubernetes"
}

data "kubernetes_config_map_v1" "foundation_contract" {
  metadata {
    name      = "fs2-terraform-cluster-contract"
    namespace = "fs2-system"
  }
}

data "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.name
    namespace = "fs2-observability"
  }
}

data "kubernetes_resource" "envoyproxy_crd" {
  api_version = "apiextensions.k8s.io/v1"
  kind        = "CustomResourceDefinition"
  metadata {
    name = "envoyproxies.gateway.envoyproxy.io"
  }
}

data "kubernetes_resource" "dcgm_daemonset" {
  api_version = "apps/v1"
  kind        = "DaemonSet"
  metadata {
    name      = "nebius-dcgm"
    namespace = "kube-system"
  }
}

resource "terraform_data" "cluster_contract" {
  input = {
    cluster_id                       = var.cluster_id
    cluster_name                     = var.cluster_name
    kube_context                     = var.kube_context
    kube_system_uid                  = var.kube_system_uid
    model_profile                    = var.deployment_profile
    accelerator_profile              = var.accelerator_pool_contract.profile
    project_sha256                   = nonsensitive(sha256(var.project_id))
    target_contract                  = var.target_contract
    target_sha256                    = local.target_contract_sha256
    target_region                    = local.selected_target.region
    run_id                           = var.run_id
    accelerator_pool_contract        = var.accelerator_pool_contract
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    infrastructure_contract          = var.infrastructure_contract
    infrastructure_contract_sha256   = local.infrastructure_contract_sha256
    public_edge_contract             = var.public_edge_contract
  }

  lifecycle {
    precondition {
      condition = (
        try(
          local.public_edge_enabled &&
          var.public_edge_contract.transport == "public-https" &&
          var.public_edge_contract.public_origin == format("https://%s", var.public_edge_contract.public_ipv4_address) &&
          var.public_edge_contract.allocation_project_id == nonsensitive(var.project_id) &&
          can(regex("^vpcallocation-[a-z0-9]+$", var.public_edge_contract.allocation_id)) &&
          can(cidrhost(format("%s/32", var.public_edge_contract.public_ipv4_address), 0)) &&
          !var.public_edge_contract.port_forward.enabled &&
          length(var.public_edge_contract.security_group_destination_ports) == 6 &&
          var.acme_email != null,
          false,
        )
        ) || (
        try(
          !local.public_edge_enabled &&
          var.public_edge_contract.transport == "kubectl-port-forward" &&
          var.public_edge_contract.public_origin == null &&
          var.public_edge_contract.allocation_project_id == null &&
          var.public_edge_contract.allocation_id == null &&
          var.public_edge_contract.public_ipv4_address == null &&
          var.public_edge_contract.port_forward.enabled &&
          var.public_edge_contract.port_forward.bind_address == "127.0.0.1" &&
          var.public_edge_contract.port_forward.application_origin == format("http://localhost:%d", var.public_edge_contract.port_forward.operator_proxy_port) &&
          var.public_edge_contract.port_forward.operator_endpoint == format("http://127.0.0.1:%d", var.public_edge_contract.port_forward.operator_proxy_port) &&
          alltrue([
            for port in [
              var.public_edge_contract.port_forward.control_plane_local_port,
              var.public_edge_contract.port_forward.admin_console_local_port,
              var.public_edge_contract.port_forward.operator_proxy_port,
            ] : floor(port) == port && port >= 1024 && port <= 65535
          ]) &&
          length(toset([
            var.public_edge_contract.port_forward.control_plane_local_port,
            var.public_edge_contract.port_forward.admin_console_local_port,
            var.public_edge_contract.port_forward.operator_proxy_port,
          ])) == 3 &&
          length(var.public_edge_contract.security_group_destination_ports) == 0 &&
          var.acme_email == null,
          false,
        )
      )
      error_message = "public_edge_contract and ACME inputs do not match the exact public or internal-only topology."
    }
    precondition {
      condition     = local.public_edge_enabled || !data.terraform_remote_state.foundation.outputs.grafana_publication_contract.enabled
      error_message = "internal-only mode requires the foundation public Grafana route to remain disabled."
    }
    precondition {
      condition     = abspath(var.kubeconfig_path) == local.expected_kubeconfig_path
      error_message = "kubeconfig_path must be the exact run-owned <run_root>/kubeconfig file."
    }
    precondition {
      condition     = var.kube_context == var.cluster_name
      error_message = "kube_context must equal the exact bounded cluster_name emitted by infrastructure."
    }
    precondition {
      condition = (
        local.selected_context != null &&
        local.selected_cluster != null &&
        local.selected_api_server != null &&
        strcontains(local.selected_api_server, var.cluster_id)
      )
      error_message = "The exact named kubeconfig context must select an API server authority containing cluster_id."
    }
    precondition {
      condition     = data.kubernetes_namespace_v1.kube_system.metadata[0].uid == var.kube_system_uid
      error_message = "The selected Kubernetes API does not have the reviewed kube-system UID."
    }
    precondition {
      condition = (
        data.kubernetes_service_v1.kubernetes_api.metadata[0].name == "kubernetes" &&
        data.kubernetes_service_v1.kubernetes_api.metadata[0].namespace == "default" &&
        can(cidrhost(local.kubernetes_api_service_cidr, 0)) &&
        length(local.kubernetes_api_endpoint_ips) >= 1 &&
        alltrue([for cidr in local.kubernetes_api_egress_cidrs : can(cidrhost(cidr, 0))]) &&
        (
          length(var.admin_kubernetes_api_cidrs) == 0 ||
          var.admin_kubernetes_api_cidrs == local.kubernetes_api_egress_cidrs
        )
      )
      error_message = "Grafana and the admin reader require the default/kubernetes Service IP, ready API endpoint host routes, and target-contract private subnet fallback; an optional supplied CIDR set is an assertion and must match the complete set."
    }
    precondition {
      condition = (
        var.target_contract.project_id == nonsensitive(var.project_id) &&
        var.target_contract.source_registry_project_id == var.target_contract.source_registry.project_id
      )
      error_message = "The infrastructure target contract must bind the exact project and one source-registry project identity."
    }
    precondition {
      condition = (
        var.accelerator_pool_contract.target_region == local.selected_target.region &&
        var.accelerator_pool_contract.artifact_source.registry.id == local.selected_target.source_registry.id &&
        var.accelerator_pool_contract.artifact_source.registry.project_id == local.selected_target.source_registry.project_id &&
        var.accelerator_pool_contract.artifact_source.registry.fqdn == local.selected_target.source_registry.fqdn &&
        var.accelerator_pool_contract.artifact_source.closure_schema == jsondecode(file("${path.module}/../../catalog/profiles/source-registry-closure.json")).schema &&
        var.accelerator_pool_contract.artifact_source.closure_sha256 == filesha256("${path.module}/../../catalog/profiles/source-registry-closure.json")
      )
      error_message = "The authoritative v2 accelerator contract differs from the selected target or reviewed artifact source identity."
    }
    precondition {
      condition = (
        var.infrastructure_contract == local.expected_infrastructure_contract &&
        local.legacy_infrastructure_contract_matches_v2
      )
      error_message = "The optional legacy infrastructure contract differs from the reviewed v1 fixture or authoritative v2 accelerator contract."
    }
    precondition {
      condition = (
        data.terraform_remote_state.foundation.outputs.cluster_contract.cluster_id == var.cluster_id &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.cluster_name == var.cluster_name &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.kube_context == var.kube_context &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.kube_system_uid == var.kube_system_uid &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.project_sha256 == nonsensitive(sha256(var.project_id)) &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.target_contract == var.target_contract &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.target_sha256 == local.target_contract_sha256 &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.target_region == local.selected_target.region &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.run_id == var.run_id &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.accelerator_pool_contract == var.accelerator_pool_contract &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.accelerator_pool_contract_sha256 == local.accelerator_pool_contract_sha256 &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.infrastructure_contract == var.infrastructure_contract &&
        data.terraform_remote_state.foundation.outputs.cluster_contract.infrastructure_contract_sha256 == local.infrastructure_contract_sha256
      )
      error_message = "Foundation state must match the selected cluster plus the exact authoritative v2 and optional legacy infrastructure contracts."
    }
    precondition {
      condition = (
        data.kubernetes_config_map_v1.foundation_contract.data.schema == "fs2-serve.nebius.ai/terraform-cluster-contract/v2" &&
        data.kubernetes_config_map_v1.foundation_contract.data.cluster_id == var.cluster_id &&
        data.kubernetes_config_map_v1.foundation_contract.data.cluster_name == var.cluster_name &&
        data.kubernetes_config_map_v1.foundation_contract.data.project_sha256 == nonsensitive(sha256(var.project_id)) &&
        data.kubernetes_config_map_v1.foundation_contract.data.target_sha256 == local.target_contract_sha256 &&
        data.kubernetes_config_map_v1.foundation_contract.data.target_region == local.selected_target.region &&
        data.kubernetes_config_map_v1.foundation_contract.data.run_id == var.run_id &&
        data.kubernetes_config_map_v1.foundation_contract.data.stage == "foundation" &&
        data.kubernetes_config_map_v1.foundation_contract.data.accelerator_pool_contract_schema == var.accelerator_pool_contract.schema &&
        data.kubernetes_config_map_v1.foundation_contract.data.accelerator_pool_contract_sha256 == local.accelerator_pool_contract_sha256 &&
        data.kubernetes_config_map_v1.foundation_contract.data.accelerator_pool_ids_json == jsonencode(local.accelerator_pool_ids) &&
        data.kubernetes_config_map_v1.foundation_contract.data.accelerator_pool_capacity_json == jsonencode(local.accelerator_pool_capacity_view) &&
        data.kubernetes_config_map_v1.foundation_contract.data.source_commit == var.accelerator_pool_contract.source_commit &&
        data.kubernetes_config_map_v1.foundation_contract.data.infrastructure_project_id == nonsensitive(var.project_id) &&
        data.kubernetes_config_map_v1.foundation_contract.data.source_registry_id == var.accelerator_pool_contract.artifact_source.registry.id &&
        data.kubernetes_config_map_v1.foundation_contract.data.source_registry_project_id == var.accelerator_pool_contract.artifact_source.registry.project_id &&
        data.kubernetes_config_map_v1.foundation_contract.data.source_registry_fqdn == var.accelerator_pool_contract.artifact_source.registry.fqdn &&
        data.kubernetes_config_map_v1.foundation_contract.data.source_registry_region == var.accelerator_pool_contract.artifact_source.registry.region &&
        data.kubernetes_config_map_v1.foundation_contract.data.artifact_closure_schema == var.accelerator_pool_contract.artifact_source.closure_schema &&
        data.kubernetes_config_map_v1.foundation_contract.data.artifact_closure_sha256 == var.accelerator_pool_contract.artifact_source.closure_sha256 &&
        data.kubernetes_config_map_v1.foundation_contract.data.capacity_profile == var.accelerator_pool_contract.profile &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_floor_profile == var.accelerator_pool_contract.floor_profile &&
        data.kubernetes_config_map_v1.foundation_contract.data.maximum_gpus == tostring(local.accelerator_pool_maximum_gpus) &&
        data.kubernetes_config_map_v1.foundation_contract.data.minimum_gpus == tostring(local.accelerator_pool_minimum_gpus)
      )
      error_message = "The in-cluster immutable contract must match the selected cluster and exact v2 accelerator source, target, pool, flavor, and capacity topology."
    }
    precondition {
      condition = !local.legacy_infrastructure_contract_enabled ? true : (
        data.kubernetes_config_map_v1.foundation_contract.data.infrastructure_contract_sha256 == local.infrastructure_contract_sha256 &&
        data.kubernetes_config_map_v1.foundation_contract.data.shared_cache_size_gib == tostring(var.infrastructure_contract.capacity.shared_cache_size_gib) &&
        data.kubernetes_config_map_v1.foundation_contract.data.system_nodes == tostring(var.infrastructure_contract.capacity.system.nodes) &&
        data.kubernetes_config_map_v1.foundation_contract.data.system_max_surge == tostring(var.infrastructure_contract.capacity.system.max_surge) &&
        data.kubernetes_config_map_v1.foundation_contract.data.system_max_unavailable == tostring(var.infrastructure_contract.capacity.system.max_unavailable) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_1x_gpus_per_node == tostring(var.infrastructure_contract.capacity.gpu_b300_1x.gpus_per_node) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_1x_min_nodes == tostring(var.infrastructure_contract.capacity.gpu_b300_1x.min_nodes) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_1x_max_nodes == tostring(var.infrastructure_contract.capacity.gpu_b300_1x.max_nodes) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_8x_gpus_per_node == tostring(var.infrastructure_contract.capacity.gpu_b300_8x.gpus_per_node) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_8x_min_nodes == tostring(var.infrastructure_contract.capacity.gpu_b300_8x.min_nodes) &&
        data.kubernetes_config_map_v1.foundation_contract.data.gpu_b300_8x_max_nodes == tostring(var.infrastructure_contract.capacity.gpu_b300_8x.max_nodes)
      )
      error_message = "The optional legacy B300 fields in the immutable foundation contract must match the supplied v1 compatibility view."
    }
    precondition {
      condition = (
        (!local.ngc_api_key_required || var.ngc_api_key != null) &&
        (!(local.model_nvcr_credentials_required || local.dcgm_nvcr_credentials_required) || var.nvcrio_dockerconfigjson != null)
      )
      error_message = "The selected NIM models require ngc_api_key and nvcrio_dockerconfigjson; the full-catalog DCGM exporter independently requires nvcrio_dockerconfigjson."
    }
    precondition {
      condition     = try(data.kubernetes_resource.envoyproxy_crd.object.metadata.name, "") == "envoyproxies.gateway.envoyproxy.io"
      error_message = "The foundation is missing the EnvoyProxy CRD from Envoy Gateway v1.8.3."
    }
    precondition {
      condition     = try(data.kubernetes_resource.dcgm_daemonset.object.metadata.name, "") == "nebius-dcgm"
      error_message = "The expected Nebius-managed DCGM hostengine DaemonSet is absent."
    }
    precondition {
      condition     = try(tonumber(data.kubernetes_resource.dcgm_daemonset.object.status.desiredNumberScheduled), -1) == 0
      error_message = "The Nebius hostengine has scheduled Pods; stop before installing the standalone Terraform-owned exporter."
    }
  }
}
