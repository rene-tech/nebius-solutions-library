output "public_endpoint" {
  description = "Terraform-owned HTTPS inference and MCP origin."
  value       = local.public_edge_enabled ? local.public_base_url : null
}

output "port_forward_contract" {
  description = "Run-scoped loopback acceptance endpoint, or null when a public edge is enabled."
  value       = local.public_edge_enabled ? null : var.public_edge_contract.port_forward
}

output "mcp_endpoint_url" {
  description = "Resolved Streamable HTTP MCP endpoint. Internal-only deployments require the run-scoped operator proxy described by port_forward_contract."
  value       = "${trimsuffix(local.public_base_url, "/")}/mcp"
}

output "inference_base_url" {
  description = "OpenAI-compatible inference base URL."
  value       = "${trimsuffix(local.public_base_url, "/")}/v1"
}

output "admin_web_interface_url" {
  description = "Resolved admin web-interface URL. Internal-only deployments require the run-scoped operator proxy described by port_forward_contract."
  value       = var.admin_console == null ? null : "${trimsuffix(local.public_base_url, "/")}/admin/"
}

output "admin_token" {
  description = "Private disposable admin credential used only to mint/revoke scoped PATs through the cluster-internal admin API; it is not valid public /v1 or /mcp authorization."
  value       = random_password.admin_token.result
  sensitive   = true
}

output "admin_bootstrap_token" {
  description = "Bootstrap credential for creating the initial admin browser session; it is not a public inference credential."
  value       = random_password.admin_token.result
  sensitive   = true
}

output "mcp_access_token" {
  description = "Terraform-owned scoped PAT provisioned in the durable control-plane token store for MCP and inference bootstrap access."
  value       = local.bootstrap_access_token
  sensitive   = true
}

output "inference_access_token" {
  description = "Alias of mcp_access_token for OpenAI-compatible inference clients; the bootstrap PAT intentionally grants both MCP and inference scopes."
  value       = local.bootstrap_access_token
  sensitive   = true
}

output "grafana_url" {
  description = "Published native-login Grafana URL, or null when publication is disabled."
  value       = local.grafana_publication.enabled ? local.grafana_publication.external_url : null
}

output "grafana_admin_username" {
  description = "Grafana bootstrap username read from the foundation-owned existing Secret."
  value       = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.user_key]
  sensitive   = true
}

output "grafana_admin_password" {
  description = "Grafana bootstrap password read from the foundation-owned existing Secret."
  value       = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.password_key]
  sensitive   = true
}

output "cluster_id" {
  description = "Nebius Managed Kubernetes cluster identifier."
  value       = var.cluster_id
}

output "cluster_name" {
  description = "Nebius Managed Kubernetes cluster name and kubeconfig context."
  value       = var.cluster_name
}

output "project_id" {
  description = "Nebius project containing this deployment."
  value       = nonsensitive(var.project_id)
}

output "region" {
  description = "Nebius region selected by the target contract."
  value       = local.selected_target.region
}

output "kubeconfig_command" {
  description = "Recreate the run-scoped kubeconfig with its exact cluster ID and context."
  value = format(
    "KUBECONFIG=%q nebius mk8s cluster get-credentials --id %q --external --force --context-name %q",
    var.kubeconfig_path,
    var.cluster_id,
    var.kube_context,
  )
}

output "access_bundle" {
  description = "Sensitive post-apply connection bundle. Request it explicitly and never place it in logs or tickets."
  sensitive   = true
  value = {
    schema = "fs2-serve.nebius.ai/access-bundle/v1"
    cluster = {
      project_id   = nonsensitive(var.project_id)
      region       = local.selected_target.region
      cluster_id   = var.cluster_id
      cluster_name = var.cluster_name
      kube_context = var.kube_context
      kubeconfig_command = format(
        "KUBECONFIG=%q nebius mk8s cluster get-credentials --id %q --external --force --context-name %q",
        var.kubeconfig_path,
        var.cluster_id,
        var.kube_context,
      )
    }
    endpoints = {
      admin_portal_url   = var.admin_console == null ? null : "${trimsuffix(local.public_base_url, "/")}/admin/"
      mcp_url            = "${trimsuffix(local.public_base_url, "/")}/mcp"
      inference_base_url = "${trimsuffix(local.public_base_url, "/")}/v1"
      grafana_url        = local.grafana_publication.enabled ? local.grafana_publication.external_url : null
    }
    credentials = {
      admin_bootstrap_token  = random_password.admin_token.result
      mcp_inference_token    = local.bootstrap_access_token
      inference_access_token = local.bootstrap_access_token
      grafana = {
        username = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.user_key]
        password = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.password_key]
      }
    }
    mcp_access = {
      principal_id    = local.bootstrap_access_principal
      tenant_id       = local.selected_target.tenant_id
      scopes          = local.bootstrap_access_scopes
      models          = local.bootstrap_access_models
      max_concurrency = 32
    }
  }
}

output "deployment_contract" {
  value = {
    profile                          = var.deployment_profile
    canonical_routes                 = local.selected_model_ids
    canonical_route_count            = length(local.selected_model_ids)
    model_manifest_count             = length(local.model_manifests)
    keeper_manifest_count            = length(local.keeper_manifests)
    keda_scaledobject_count          = length(local.model_scalers)
    public_edge_mode                 = var.public_edge_contract.mode
    public_allocation_id             = var.public_edge_contract.allocation_id
    target_contract                  = var.target_contract
    target_region                    = local.selected_target.region
    target_sha256                    = local.target_contract_sha256
    accelerator_pool_contract        = var.accelerator_pool_contract
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    infrastructure_contract          = var.infrastructure_contract
    infrastructure_contract_sha256   = local.infrastructure_contract_sha256
  }
}

output "dynamic_model_handoff_receipt" {
  description = "Copy this non-secret receipt into deployment.dynamic_models.handoff_receipt only after the explicit workload_owner=released apply has completed."
  value = (
    var.model_controller.workload_owner == "released" ?
    local.model_controller_expected_handoff_receipt :
    null
  )
}

output "dynamic_model_contract" {
  description = "Non-secret derived controller ownership, catalog, bootstrap, and immutable contract identities."
  value = {
    enabled                     = var.model_controller.enabled
    writes_enabled              = var.model_controller.writes_enabled
    workload_owner              = var.model_controller.workload_owner
    catalog_model_ids           = local.selected_model_ids
    controller_model_ids        = local.model_controller_dynamic_model_ids
    bootstrap_model_ids         = sort(tolist(var.model_controller.bootstrap_model_ids))
    ineligible_models           = local.model_controller_ineligible_reasons
    static_ineligible_model_ids = sort(keys(local.model_controller_ineligible_reasons))
    envelope_revision           = var.model_controller.enabled ? local.model_controller_envelope.revision : null
    renderer_bundles_sha256     = var.model_controller.enabled ? sha256(local.model_controller_bundles_json) : null
    terraform_manifest_count    = length(local.terraform_owned_model_manifests)
    terraform_scaler_count      = length(local.terraform_owned_model_scalers)
    bootstrap_job_enabled       = local.model_controller_bootstrap_enabled
    unsupported_gvks_retained = sort(distinct([
      for document in values(local.terraform_owned_model_manifests) : "${document.manifest.apiVersion}/${document.manifest.kind}"
      if(
        var.model_controller.workload_owner != "terraform" &&
        !contains(
          local.model_controller_supported_template_gvks,
          "${document.manifest.apiVersion}/${document.manifest.kind}",
        )
      )
    ]))
  }
}

output "model_autoscaling_contract" {
  description = "Non-secret replica-owner, hot-floor, timing, and exact route-to-Deployment contract."
  value = {
    mode                      = var.model_scaling_mode
    replica_owner             = var.model_scaling_mode == "keda" ? "keda" : "terraform"
    activation_handshake      = "disabled-lean-route"
    hot_model_ids             = sort(tolist(var.hot_model_ids))
    polling_interval_seconds  = var.keda_polling_interval_seconds
    cooldown_period_seconds   = var.keda_cooldown_period_seconds
    prometheus_server_address = var.model_scaling_mode == "keda" ? local.prometheus_server_address : null
    targets = {
      for model_id, target in local.model_scalers : model_id => {
        deployment   = target.deployment
        service      = target.service
        gpu_count    = target.gpu_count
        min_replicas = target.min_replicas
        max_replicas = target.max_replicas
      }
    }
  }
}

output "dcgm_attribution_contract" {
  description = "Non-secret Terraform-owned DCGM collection/scrape provenance for nominal attempt-bound Prometheus proxy evidence."
  value = {
    schema                                 = "fs2-serve.nebius.ai/dcgm-attribution-terraform/v1"
    campaign_enabled                       = var.enable_dcgm_cold_start_campaign
    attribution_metric_collection_interval = local.dcgm_collection_interval
    scrape_interval                        = local.dcgm_scrape_interval
    scrape_timeout                         = local.dcgm_scrape_timeout
    campaign_metrics                       = local.dcgm_campaign_metrics
    minimum_nominal_window_seconds         = local.dcgm_minimum_nominal_window
    missing_sample_policy                  = "FAIL_CLOSED_NO_ESTIMATE"
  }
}

output "managed_resource_count" {
  description = "Expected concrete managed-address count for exact plan review."
  value = (
    # Profile-independent identity, credential, database, queue, control-plane,
    # and Grafana egress addresses. Profile-shaped collections stay explicit.
    47 +
    (local.ngc_api_key_required ? 1 : 0) +
    (local.model_nvcr_credentials_required ? 1 : 0) +
    (local.dcgm_nvcr_credentials_required ? 1 : 0) +
    (var.deployment_profile == "full_catalog" ? 1 : 0) +
    length(local.terraform_owned_model_manifests) +
    length(local.keeper_manifests) +
    length(local.terraform_owned_model_scalers) +
    (var.model_controller.enabled ? 2 : 0) +
    (local.model_controller_bootstrap_enabled ? 3 : 0) +
    (local.admin_configuration_enabled ? 1 : 0) +
    (data.terraform_remote_state.foundation.outputs.grafana_publication_contract.enabled ? 2 : 0) +
    (var.run_acceptance_job ? 4 : 0) +
    (var.run_acceptance_job && var.deployment_profile == "full_catalog" ? 1 : 0)
  )
}

output "sensitive_state_notice" {
  value = "Generated admin, MCP/inference, Grafana, database, and cryptographic bootstrap material is stored in the run-owned local workloads state; keep the run root mode 0700/state files mode 0600 and destroy it after acceptance."
}
