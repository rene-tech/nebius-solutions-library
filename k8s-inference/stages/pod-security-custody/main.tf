provider "kubernetes" {
  config_path    = pathexpand(var.owner_kubeconfig_path)
  config_context = var.owner_context
}

data "external" "verified_trust" {
  program = ["python3", "${path.module}/../../scripts/verify_sai07_custody_trust.py"]
  query = {
    trust_lock_path              = "${path.module}/custody-trust-lock.json"
    iam_boundary_receipt_path    = var.iam_boundary_receipt_path
    backend_custody_receipt_path = var.backend_custody_receipt_path
    owner_kubeconfig_path        = var.owner_kubeconfig_path
    owner_context                = var.owner_context
  }
}

data "external" "verified_bundle" {
  program = ["python3", "${path.module}/../../scripts/verify_sai07_custody_manifest_bundle_v2.py"]
  query = {
    bundle_path           = var.manifest_bundle_path
    verified_trust_json   = jsonencode(data.external.verified_trust.result)
    owner_kubeconfig_path = var.owner_kubeconfig_path
    owner_context         = var.owner_context
  }
}

locals {
  custody_manifests_all    = jsondecode(data.external.verified_bundle.result.manifests_json)
  token_anchor_key         = "v1/Secret/fs2-system/fs2-pod-security-token-anchor"
  custody_manifests        = { for key, manifest in local.custody_manifests_all : key => manifest if key != local.token_anchor_key }
  custody_existing_imports = jsondecode(data.external.verified_bundle.result.existing_imports_json)
}

# The anchor is deliberately a typed POST create rather than an SSA PATCH.
# Kubernetes therefore provides the atomic absent->created boundary: if the
# name appears after the signed state inventory and immediate rereads, creation
# fails instead of adopting or modifying an attacker-supplied Secret.
resource "kubernetes_secret_v1" "token_anchor" {
  metadata {
    name      = "fs2-pod-security-token-anchor"
    namespace = "fs2-system"
    labels = {
      "security.fs2.nebius.ai/custody-owner" = "external"
    }
  }
  immutable = true
  type      = "Opaque"
  data      = {}

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.external.verified_bundle.result.valid == "true" &&
        try(local.custody_manifests_all[local.token_anchor_key].immutable, false) == true &&
        try(local.custody_manifests_all[local.token_anchor_key].type, "") == "Opaque" &&
        try(local.custody_manifests_all[local.token_anchor_key].data, null) == {}
      )
      error_message = "The signed bundle must require the exact additive immutable empty token anchor."
    }
  }
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
  depends_on = [
    kubernetes_secret_v1.token_anchor,
  ]

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
    precondition {
      condition     = data.external.verified_bundle.result.platform_state_object_count == data.external.verified_bundle.result.refreshed_live_object_count
      error_message = "Every adopted platform-state object must be freshly reread before SSA."
    }
  }
}
