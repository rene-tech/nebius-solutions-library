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

output "admin_web_interface_url" {
  description = "Resolved admin web-interface URL. Internal-only deployments require the run-scoped operator proxy described by port_forward_contract."
  value       = var.admin_console == null ? null : "${trimsuffix(local.public_base_url, "/")}/admin/"
}

output "admin_token" {
  description = "Private disposable admin credential used only to mint/revoke scoped PATs through the cluster-internal admin API; it is not valid public /v1 or /mcp authorization."
  value       = random_password.admin_token.result
  sensitive   = true
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

output "model_autoscaling_contract" {
  description = "Non-secret replica-owner, hot-floor, timing, and exact route-to-Deployment contract."
  value = {
    mode                       = var.model_scaling_mode
    replica_owner              = var.model_scaling_mode == "keda" ? "keda" : "terraform"
    activation_handshake       = "disabled-lean-route"
    hot_model_ids              = sort(tolist(var.hot_model_ids))
    polling_interval_seconds   = var.keda_polling_interval_seconds
    cooldown_period_seconds    = var.keda_cooldown_period_seconds
    fallback_failure_threshold = var.keda_fallback_failure_threshold
    prometheus_server_address  = var.model_scaling_mode == "keda" ? local.prometheus_server_address : null
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
    43 +
    (local.ngc_api_key_required ? 1 : 0) +
    (local.model_nvcr_credentials_required ? 1 : 0) +
    (local.dcgm_nvcr_credentials_required ? 1 : 0) +
    (var.deployment_profile == "full_catalog" ? 1 : 0) +
    length(local.model_manifests) +
    length(local.keeper_manifests) +
    length(local.model_scalers) +
    (local.admin_configuration_enabled ? 1 : 0) +
    (data.terraform_remote_state.foundation.outputs.grafana_publication_contract.enabled ? 2 : 0) +
    (var.run_acceptance_job ? 4 : 0) +
    (var.run_acceptance_job && var.deployment_profile == "full_catalog" ? 1 : 0)
  )
}

output "sensitive_state_notice" {
  value = "Generated admin, database, and cryptographic bootstrap material is stored in the run-owned local workloads state; keep the run root mode 0700/state files mode 0600 and destroy it after acceptance."
}
