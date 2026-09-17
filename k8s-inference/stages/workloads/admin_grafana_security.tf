locals {
  grafana_edge_rate_limit = {
    requests = 200
    unit     = "Second"
  }
}

resource "kubernetes_manifest" "grafana_security_policy" {
  count = local.grafana_publication.enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "SecurityPolicy"
    metadata = {
      name      = "fs2-admin-grafana-client-cidrs"
      namespace = local.grafana_publication.route_namespace
      labels    = local.grafana_publication_labels
    }
    spec = {
      targetRefs = [{
        group = "gateway.networking.k8s.io"
        kind  = "HTTPRoute"
        name  = "fs2-admin-grafana"
      }]
      authorization = {
        defaultAction = "Deny"
        rules = [{
          action = "Allow"
          principal = {
            clientCIDRs = sort(tolist(var.grafana_allowed_source_cidrs))
          }
        }]
      }
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana-security"
  }

  lifecycle {
    precondition {
      condition = (
        length(var.grafana_allowed_source_cidrs) >= 1 &&
        length(var.grafana_allowed_source_cidrs) <= 8 &&
        !contains(var.grafana_allowed_source_cidrs, "0.0.0.0/0")
      )
      error_message = "Published Grafana requires a non-empty, bounded client CIDR allow-list; universal access is forbidden."
    }
  }

  depends_on = [kubernetes_manifest.grafana_http_route]
}

resource "kubernetes_manifest" "grafana_backend_traffic_policy" {
  count = local.grafana_publication.enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "BackendTrafficPolicy"
    metadata = {
      name      = "fs2-admin-grafana-edge-limit"
      namespace = local.grafana_publication.route_namespace
      labels    = local.grafana_publication_labels
    }
    spec = {
      mergeType = "StrategicMerge"
      targetRefs = [{
        group = "gateway.networking.k8s.io"
        kind  = "HTTPRoute"
        name  = "fs2-admin-grafana"
      }]
      rateLimit = {
        local = {
          rules = [{ limit = local.grafana_edge_rate_limit }]
        }
      }
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana-rate-limit"
  }

  depends_on = [kubernetes_manifest.grafana_http_route]
}

output "grafana_edge_security_contract" {
  description = "Non-secret authorization and abuse-protection contract for the optional public Grafana route."
  value = local.grafana_publication.enabled ? {
    authorization = {
      default_action       = "Deny"
      allowed_source_cidrs = sort(tolist(var.grafana_allowed_source_cidrs))
      policy               = "fs2-system/fs2-admin-grafana-client-cidrs"
    }
    rate_limit = {
      policy   = "fs2-system/fs2-admin-grafana-edge-limit"
      requests = local.grafana_edge_rate_limit.requests
      unit     = local.grafana_edge_rate_limit.unit
    }
    target = "fs2-system/fs2-admin-grafana"
  } : null
}
