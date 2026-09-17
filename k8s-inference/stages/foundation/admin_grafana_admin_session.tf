variable "grafana_admin_session_publication" {
  description = "Staged Grafana publication protected by the control-plane admin session. Prepare exposes only a direct 403 quarantine response; attach is separately status-gated."
  type = object({
    phase             = optional(string, "disabled")
    external_base_url = optional(string, "")
    service_name      = optional(string)
    service_port      = optional(number, 80)
  })
  default = {}

  validation {
    condition = contains(
      ["disabled", "prepare", "attach"],
      var.grafana_admin_session_publication.phase,
    )
    error_message = "grafana_admin_session_publication.phase must be disabled, prepare, or attach."
  }

  validation {
    condition = (
      var.grafana_admin_session_publication.phase == "disabled" ||
      can(regex(
        "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$",
        var.grafana_admin_session_publication.external_base_url,
      ))
    )
    error_message = "grafana_admin_session_publication.external_base_url must be one HTTPS origin without a path when prepare or attach is selected."
  }

  validation {
    condition = (
      var.grafana_admin_session_publication.service_name == null ||
      (
        length(var.grafana_admin_session_publication.service_name) <= 253 &&
        can(regex(
          "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$",
          var.grafana_admin_session_publication.service_name,
        )) &&
        strcontains(var.grafana_admin_session_publication.service_name, "grafana")
      )
    )
    error_message = "grafana_admin_session_publication.service_name must be a valid Grafana Kubernetes Service name when overridden."
  }

  validation {
    condition = (
      floor(var.grafana_admin_session_publication.service_port) == var.grafana_admin_session_publication.service_port &&
      var.grafana_admin_session_publication.service_port >= 1 &&
      var.grafana_admin_session_publication.service_port <= 65535
    )
    error_message = "grafana_admin_session_publication.service_port must be an integer from 1 through 65535."
  }
}

locals {
  grafana_admin_session_publication_enabled = var.grafana_admin_session_publication.phase != "disabled"
  grafana_admin_session_publication_path    = "/admin/observability/grafana"
  grafana_admin_session_service_name = coalesce(
    var.grafana_admin_session_publication.service_name,
    "fs2-${var.run_id}-monitoring-grafana",
  )
  grafana_admin_session_root_url = local.grafana_admin_session_publication_enabled ? format(
    "%s%s/",
    trimsuffix(var.grafana_admin_session_publication.external_base_url, "/"),
    local.grafana_admin_session_publication_path,
  ) : null
  grafana_admin_session_publication_values = local.grafana_admin_session_publication_enabled ? {
    grafana = {
      "grafana.ini" = {
        server = {
          root_url            = local.grafana_admin_session_root_url
          serve_from_sub_path = true
        }
        "auth.anonymous" = {
          enabled = false
        }
        auth = {
          disable_login_form = false
        }
      }
    }
  } : {}
}

output "grafana_admin_session_publication_contract" {
  description = "Non-secret, two-phase Grafana publication contract consumed by the workloads-stage external-authorization gate."
  value = {
    schema          = "fs2-serve.nebius.ai/grafana-admin-session-publication/v2"
    phase           = var.grafana_admin_session_publication.phase
    enabled         = local.grafana_admin_session_publication_enabled
    namespace       = "fs2-observability"
    service_name    = local.grafana_admin_session_service_name
    service_port    = var.grafana_admin_session_publication.service_port
    path            = local.grafana_admin_session_publication_path
    external_url    = local.grafana_admin_session_publication_enabled ? trimsuffix(local.grafana_admin_session_root_url, "/") : null
    root_url        = local.grafana_admin_session_root_url
    authentication  = "fs2-admin-session+grafana-native"
    gateway_name    = "public"
    listener_name   = "public-https"
    route_namespace = "fs2-system"
    ext_auth = {
      service_name  = "fs2-serve-control-plane"
      service_port  = 8080
      path_override = "/admin/api/v1/grafana-authorization"
      fail_open      = false
    }
    required_policy_conditions = {
      Accepted = "True"
    }
    required_route_conditions = {
      Accepted     = "True"
      ResolvedRefs = "True"
    }
    raw_backends_public = false
  }
}
