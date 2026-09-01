locals {
  grafana_publication = data.terraform_remote_state.foundation.outputs.grafana_publication_contract
  grafana_publication_labels = merge(local.common_labels, {
    "app.kubernetes.io/component"                  = "admin-observability"
    "fs2-serve.nebius.ai/public-backend"           = "grafana-only"
    "fs2-serve.nebius.ai/required-parent-statuses" = "Accepted-True_ResolvedRefs-True"
  })
}

resource "kubernetes_manifest" "grafana_reference_grant" {
  count = local.grafana_publication.enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.networking.k8s.io/v1beta1"
    kind       = "ReferenceGrant"
    metadata = {
      name      = "fs2-admin-grafana"
      namespace = local.grafana_publication.namespace
      labels    = local.grafana_publication_labels
    }
    spec = {
      from = [{
        group     = "gateway.networking.k8s.io"
        kind      = "HTTPRoute"
        namespace = local.grafana_publication.route_namespace
      }]
      to = [{
        group = ""
        kind  = "Service"
        name  = local.grafana_publication.service_name
      }]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana"
  }

  lifecycle {
    precondition {
      condition = (
        local.grafana_publication.schema == "fs2-serve.nebius.ai/grafana-publication/v1" &&
        local.grafana_publication.path == "/admin/observability/grafana" &&
        local.grafana_publication.gateway_name == "public" &&
        local.grafana_publication.listener_name == "public-https" &&
        local.grafana_publication.namespace == "fs2-observability" &&
        local.grafana_publication.route_namespace == "fs2-system" &&
        local.grafana_publication.login_mode == "grafana-native" &&
        !local.grafana_publication.raw_backends_public &&
        strcontains(local.grafana_publication.service_name, "grafana") &&
        !strcontains(local.grafana_publication.service_name, "prometheus") &&
        !strcontains(local.grafana_publication.service_name, "loki")
      )
      error_message = "Foundation Grafana publication output is not the reviewed native-login, Grafana-only contract."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "grafana_http_route" {
  count = local.grafana_publication.enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "HTTPRoute"
    metadata = {
      name      = "fs2-admin-grafana"
      namespace = local.grafana_publication.route_namespace
      labels    = local.grafana_publication_labels
      annotations = {
        "fs2-serve.nebius.ai/acceptance" = "parent Accepted=True and ResolvedRefs=True"
      }
    }
    spec = {
      parentRefs = [{
        group       = "gateway.networking.k8s.io"
        kind        = "Gateway"
        name        = local.grafana_publication.gateway_name
        namespace   = local.grafana_publication.route_namespace
        sectionName = local.grafana_publication.listener_name
      }]
      rules = [{
        matches = [{
          path = {
            type  = "PathPrefix"
            value = local.grafana_publication.path
          }
        }]
        backendRefs = [{
          group     = ""
          kind      = "Service"
          name      = local.grafana_publication.service_name
          namespace = local.grafana_publication.namespace
          port      = local.grafana_publication.service_port
          weight    = 1
        }]
      }]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana"
  }

  lifecycle {
    precondition {
      condition = (
        local.grafana_publication.external_url == "${local.public_base_url}/admin/observability/grafana" &&
        local.grafana_publication.root_url == "${local.public_base_url}/admin/observability/grafana/"
      )
      error_message = "Grafana root_url and public link must use the exact current HTTPS Gateway authority and retained subpath."
    }

    precondition {
      condition = local.grafana_publication.required_parent_conditions == {
        Accepted     = "True"
        ResolvedRefs = "True"
      }
      error_message = "Grafana HTTPRoute acceptance must require Accepted=True and ResolvedRefs=True."
    }
  }

  timeouts {
    create = "10m"
    update = "10m"
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.grafana_reference_grant,
  ]
}

output "admin_observability_links" {
  description = "Verified-link inputs for the admin UI. Only Grafana is public; raw telemetry stores stay cluster-private."
  value = {
    grafana = local.grafana_publication.enabled ? {
      label          = "Grafana"
      url            = local.grafana_publication.external_url
      authentication = "grafana-native"
    } : null
    raw_prometheus = null
    raw_loki       = null
    route_acceptance = local.grafana_publication.enabled ? {
      gateway         = "public"
      listener        = "public-https"
      conditions      = local.grafana_publication.required_parent_conditions
      reference_grant = "fs2-observability/fs2-admin-grafana"
    } : null
  }
}
