provider "kubernetes" {
  config_path    = pathexpand(var.owner_kubeconfig_path)
  config_context = var.owner_context
}

data "external" "verified_bundle" {
  program = ["python3", "${path.module}/../../scripts/verify_sai07_custody_manifest_bundle.py"]
  query = {
    bundle_path         = var.manifest_bundle_path
    public_key_path     = var.manifest_public_key_path
    public_key_sha256   = var.manifest_public_key_sha256
    key_id              = var.manifest_key_id
    cluster_id          = var.cluster_id
    kube_system_uid     = var.kube_system_uid
    owner_username      = var.owner_username
    owner_group         = var.owner_group
    platform_username   = var.platform_username
    platform_group      = var.platform_group
    iam_boundary_sha256 = var.iam_boundary_sha256
  }
}

locals {
  custody_manifests        = jsondecode(data.external.verified_bundle.result.manifests_json)
  custody_existing_imports = jsondecode(data.external.verified_bundle.result.existing_imports_json)
}

# Pre-existing objects are imported into the external state before server-side
# apply. Objects attested absent are additive creates and have no import entry.
import {
  for_each = local.custody_existing_imports
  to       = kubernetes_manifest.custody[each.key]
  id       = each.value
}

# This root has its own backend/state lifecycle and exactly one Kubernetes
# provider. It accepts no platform, receipt-operator, or short-lived reader
# kubeconfig. Existing objects are adopted with server-side apply and retained.
resource "kubernetes_manifest" "custody" {
  for_each = local.custody_manifests
  manifest = each.value

  field_manager {
    name            = "fs2-sai07-external-custody"
    force_conflicts = false
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.external.verified_bundle.result.valid == "true"
      error_message = "The separately signed custody manifest bundle is invalid."
    }
  }
}
