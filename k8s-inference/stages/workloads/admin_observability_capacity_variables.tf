variable "admin_kubernetes_api_cidrs" {
  description = "Optional fail-closed assertion for the complete API egress set: default/kubernetes Service IP, ready control-plane endpoint host routes, and the target-contract private subnet CIDR used as the stable endpoint-rotation fallback."
  type        = set(string)
  default     = []

  validation {
    condition = length(var.admin_kubernetes_api_cidrs) <= 32 && alltrue([
      for cidr in var.admin_kubernetes_api_cidrs :
      can(cidrhost(cidr, 0)) && !contains(["0.0.0.0/0", "::/0"], cidr)
    ])
    error_message = "admin_kubernetes_api_cidrs may be empty or contain at most 32 valid non-default-route API destinations."
  }
}

variable "admin_observability_links" {
  description = "Verified external HTTPS launch links. Keep Prometheus/Loki empty when authenticated Grafana is the single observability pane."
  type = object({
    allowed_hosts = set(string)
    grafana = object({
      url                     = string
      verified_external_route = bool
    })
    prometheus = object({
      url                     = string
      verified_external_route = bool
    })
    loki = object({
      url                     = string
      verified_external_route = bool
    })
  })
  default = {
    allowed_hosts = []
    grafana       = { url = "", verified_external_route = false }
    prometheus    = { url = "", verified_external_route = false }
    loki          = { url = "", verified_external_route = false }
  }

  validation {
    condition = alltrue([
      for link in [
        var.admin_observability_links.grafana,
        var.admin_observability_links.prometheus,
        var.admin_observability_links.loki,
        ] : (
        (link.url == "" && !link.verified_external_route) ||
        (
          link.url != "" &&
          link.verified_external_route &&
          can(regex("^https://[^/@?#]+(?::[0-9]{1,5})?(/[^?#]*)?$", link.url))
        )
      )
    ])
    error_message = "Every admin observability URL must be an attested, credential-free external HTTPS route; empty URLs must remain unverified."
  }

  validation {
    condition = alltrue([
      for host in var.admin_observability_links.allowed_hosts :
      can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9.]*[A-Za-z0-9])?$", host))
    ])
    error_message = "admin_observability_links.allowed_hosts must contain exact DNS hostnames without schemes, paths, ports, or wildcards."
  }

  validation {
    condition = alltrue([
      for link in [
        var.admin_observability_links.grafana,
        var.admin_observability_links.prometheus,
        var.admin_observability_links.loki,
        ] : link.url == "" || contains(
        var.admin_observability_links.allowed_hosts,
        try(regex("^https://([^/:]+)", link.url)[0], ""),
      )
    ])
    error_message = "Every admin observability link hostname must appear in allowed_hosts."
  }
}
