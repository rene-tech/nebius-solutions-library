resource "kubernetes_config_map_v1" "serving_bindings" {
  metadata {
    name      = "fs2-serve-serving-bindings-terraform"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-routing" })
    annotations = {
      "fs2.nebius.ai/catalog-sha256"   = local.catalog_digest
      "fs2.nebius.ai/inventory-sha256" = sha256(jsonencode(local.inventory))
    }
  }
  immutable = true
  data = {
    "serving-bindings.json"         = jsonencode(local.serving_bindings)
    "model-variant-promotions.json" = jsonencode(local.variant_promotions)
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "lean_routes" {
  metadata {
    name      = local.lean_routes_config_map_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-routing" })
    annotations = {
      "fs2.nebius.ai/catalog-sha256"   = local.catalog_digest
      "fs2.nebius.ai/inventory-sha256" = sha256(jsonencode(local.inventory))
    }
  }
  immutable = true
  # The Helm release mounts only lean-routes.json. Qualification remains
  # available as retained evidence without broadening the runtime schema.
  data = local.lean_routes_config_map_data

  lifecycle {
    create_before_destroy = true

    precondition {
      condition = (
        length(local.selected_runtime_ports) > 0 &&
        length(local.selected_runtime_ports) <= length(local.selected_routes) &&
        alltrue([for port in local.selected_runtime_ports : port >= 1 && port <= 65535])
      )
      error_message = "Selected model routes must resolve to a nonempty bounded set of distinct runtime ports."
    }
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "platform_contract" {
  metadata {
    name      = "fs2-terraform-workloads-contract"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  immutable = true
  data = merge({
    schema                                      = var.model_scaling_mode == "keda" ? "fs2-serve.nebius.ai/terraform-workloads-contract/v2" : "fs2-serve.nebius.ai/terraform-workloads-contract/v1"
    deployment_profile                          = var.deployment_profile
    canonical_route_count                       = tostring(length(local.selected_model_ids))
    model_manifest_count                        = tostring(length(local.model_manifests))
    keeper_manifest_count                       = tostring(length(local.keeper_manifests))
    catalog_rollout_digest                      = var.catalog_rollout_digest
    keda_scaledobject_count                     = tostring(length(local.model_scalers))
    dcgm_provider_hostengine                    = "present-inactive"
    dcgm_exporter_owner                         = var.deployment_profile == "full_catalog" ? "terraform" : "not-installed-minimal"
    dcgm_exporter_version                       = var.deployment_profile == "full_catalog" ? "4.8.3" : "none"
    dcgm_campaign_enabled                       = tostring(var.enable_dcgm_cold_start_campaign)
    dcgm_attribution_metric_collection_interval = local.dcgm_collection_interval
    dcgm_scrape_interval                        = local.dcgm_scrape_interval
    dcgm_scrape_timeout                         = local.dcgm_scrape_timeout
    run_id                                      = var.run_id
  }, local.model_autoscaling_config_map_data)
  depends_on = [terraform_data.cluster_contract]
}
