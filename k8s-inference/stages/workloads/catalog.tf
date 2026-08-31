resource "kubernetes_config_map_v1" "serving_bindings" {
  metadata {
    name      = local.serving_bindings_config_map_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-routing" })
    annotations = {
      "fs2.nebius.ai/catalog-sha256"   = local.catalog_digest
      "fs2.nebius.ai/inventory-sha256" = sha256(jsonencode(local.inventory))
    }
  }
  immutable = true
  data      = local.serving_bindings_config_map_data

  lifecycle {
    create_before_destroy = true
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
    name      = local.platform_contract_config_map_name
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  immutable = true
  data      = local.platform_contract_config_map_data

  lifecycle {
    create_before_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}
