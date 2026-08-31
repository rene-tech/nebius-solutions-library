variable "grafana_publication" {
  description = "Optional native-login Grafana publication below the existing HTTPS Gateway. Raw Prometheus and Loki services remain private."
  type = object({
    enabled           = bool
    external_base_url = string
    service_name      = optional(string)
    service_port      = optional(number, 80)
  })
  default = {
    enabled           = false
    external_base_url = ""
  }

  validation {
    condition = (
      !var.grafana_publication.enabled ||
      can(regex("^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$", var.grafana_publication.external_base_url))
    )
    error_message = "grafana_publication.external_base_url must be one HTTPS origin without a path when publication is enabled."
  }

  validation {
    condition = (
      var.grafana_publication.service_name == null ||
      (
        length(var.grafana_publication.service_name) <= 253 &&
        can(regex("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", var.grafana_publication.service_name)) &&
        strcontains(var.grafana_publication.service_name, "grafana")
      )
    )
    error_message = "grafana_publication.service_name must be a valid Grafana Kubernetes Service name when overridden."
  }

  validation {
    condition = (
      floor(var.grafana_publication.service_port) == var.grafana_publication.service_port &&
      var.grafana_publication.service_port >= 1 &&
      var.grafana_publication.service_port <= 65535
    )
    error_message = "grafana_publication.service_port must be an integer from 1 through 65535."
  }
}

locals {
  grafana_publication_path = "/admin/observability/grafana"
  grafana_service_name = coalesce(
    var.grafana_publication.service_name,
    "fs2-${var.run_id}-monitoring-grafana",
  )
  grafana_root_url = var.grafana_publication.enabled ? format(
    "%s%s/",
    trimsuffix(var.grafana_publication.external_base_url, "/"),
    local.grafana_publication_path,
  ) : null
  grafana_publication_values = var.grafana_publication.enabled ? {
    grafana = {
      "grafana.ini" = {
        server = {
          root_url            = local.grafana_root_url
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

output "grafana_publication_contract" {
  description = "Non-secret service identity and acceptance contract consumed by the workloads HTTPRoute."
  value = {
    schema          = "fs2-serve.nebius.ai/grafana-publication/v1"
    enabled         = var.grafana_publication.enabled
    namespace       = "fs2-observability"
    service_name    = local.grafana_service_name
    service_port    = var.grafana_publication.service_port
    path            = local.grafana_publication_path
    external_url    = var.grafana_publication.enabled ? trimsuffix(local.grafana_root_url, "/") : null
    root_url        = local.grafana_root_url
    login_mode      = "grafana-native"
    gateway_name    = "public"
    listener_name   = "public-https"
    route_namespace = "fs2-system"
    required_parent_conditions = {
      Accepted     = "True"
      ResolvedRefs = "True"
    }
    raw_backends_public = false
  }
}
