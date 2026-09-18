variable "public_edge_boundary" {
  description = "Owner-signed, fail-closed SAI-15 boundary chart enrollment. Null keeps the integration gate closed."
  type        = any
  default     = null
  sensitive   = true
}

variable "public_edge_boundary_enabled" {
  description = "Nonsensitive owner gate for installing the SAI-15 boundary chart."
  type        = bool
  default     = false
}

resource "helm_release" "public_edge_boundary" {
  count            = var.public_edge_boundary_enabled ? 1 : 0
  name             = "fs2-public-edge-boundary"
  namespace        = "fs2-system"
  chart            = "${local.fs2_root}/components/public-edge-boundary/chart"
  create_namespace = false
  atomic           = false
  cleanup_on_fail  = false
  wait             = true
  wait_for_jobs    = true
  timeout          = 1800

  values = [yamlencode(var.public_edge_boundary)]

  lifecycle {
    precondition {
      condition = (
        var.public_edge_boundary_enabled && var.public_edge_boundary != null &&
        can(regex("^sha256:[0-9a-f]{64}$", var.public_edge_boundary.image.digest)) &&
        length(try(var.public_edge_boundary.network.apiServerCIDRs, [])) > 0 &&
        length(try(var.public_edge_boundary.network.providerControlPlaneCIDRs, [])) > 0
      )
      error_message = "public_edge_boundary requires a digest-pinned image and owner-enrolled API-server/provider CIDRs."
    }
  }

  depends_on = [helm_release.control_plane]
}
