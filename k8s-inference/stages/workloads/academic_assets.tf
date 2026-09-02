# Tenant-private delivery of licensed academic assets.
#
# Licensed bytes (AlphaFold 3 parameters, the BindCraft PyRosetta wheel and its
# installed tree) live on a tenant-private ReadWriteMany claim and are mounted
# read-only by runtime pods.  They are never embedded in a container image and
# never placed in a general shared cache.
#
# These resources are adoptable: the canonical claim was first created live and
# is imported into this state rather than recreated, because recreating it would
# discard verified licensed bytes.  See academic-assets/scripts/adopt-live-resources.sh.

locals {
  academic_assets_enabled = var.academic_assets.enabled

  academic_common_labels = {
    "app.kubernetes.io/name"       = "academic-assets"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "app.kubernetes.io/managed-by" = "terraform"
    "fs2.nebius.ai/tenant-id"      = var.academic_assets.tenant_id
  }

  academic_runtime_labels = merge(local.academic_common_labels, {
    "fs2.nebius.ai/academic-runtime" = "true"
  })

  # Retained historical quarantine. It is declared so that no academic storage is
  # left unmanaged, and it is never mountable by a runtime.
  academic_legacy_enabled = (
    local.academic_assets_enabled && var.academic_assets.legacy_quarantine_claim.enabled
  )
}

resource "kubernetes_namespace_v1" "academic_assets" {
  count = local.academic_assets_enabled ? 1 : 0

  metadata {
    name = var.academic_assets.namespace
    labels = merge(local.academic_common_labels, {
      "kubernetes.io/metadata.name" = var.academic_assets.namespace
    })
  }

  lifecycle {
    # Adopted from a live namespace; annotations added by other controllers must
    # not cause a destroy/recreate of a namespace holding licensed data.
    ignore_changes = [metadata[0].annotations]
  }
}

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_runtime" {
  count = local.academic_assets_enabled ? 1 : 0

  wait_until_bound = false

  metadata {
    name      = var.academic_assets.runtime_claim.name
    namespace = var.academic_assets.namespace
    labels    = local.academic_runtime_labels
    annotations = {
      "fs2.nebius.ai/data-classification"  = "licensed-academic-nonredistributable"
      "fs2.nebius.ai/region"               = var.academic_assets.region
      "fs2.nebius.ai/general-shared-cache" = "false"
      "fs2.nebius.ai/delivery-mode"        = var.academic_assets.delivery.mode
    }
  }

  spec {
    access_modes       = [var.academic_assets.runtime_claim.access_mode]
    storage_class_name = var.academic_assets.runtime_claim.storage_class

    resources {
      requests = {
        storage = "${var.academic_assets.runtime_claim.storage_gib}Gi"
      }
    }
  }

  lifecycle {
    # The live claim reports the provisioner's actual capacity, which can exceed
    # the request. Destroying this claim would destroy verified licensed bytes.
    prevent_destroy = true

    ignore_changes = [
      metadata[0].annotations,
      spec[0].resources[0].requests,
      spec[0].volume_name,
    ]
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_legacy_quarantine" {
  count = local.academic_legacy_enabled ? 1 : 0

  wait_until_bound = false

  metadata {
    name      = var.academic_assets.legacy_quarantine_claim.name
    namespace = var.academic_assets.legacy_quarantine_claim.namespace
    labels = merge(local.academic_common_labels, {
      "fs2.nebius.ai/academic-runtime" = "false"
    })
    annotations = {
      "fs2.nebius.ai/data-classification" = "licensed-academic-nonredistributable"
      "fs2.nebius.ai/region"              = var.academic_assets.region
      "fs2.nebius.ai/runtime-mountable"   = "false"
      "fs2.nebius.ai/retention"           = "retain-rejected-artifact-archive"
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.academic_assets.runtime_claim.storage_class

    resources {
      requests = {
        storage = "${var.academic_assets.legacy_quarantine_claim.storage_gib}Gi"
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    ignore_changes = [
      metadata[0].annotations,
      metadata[0].labels,
      spec[0].resources[0].requests,
      spec[0].volume_name,
    ]
  }
}

# Offline runtime validation must be provably offline.  Pods that opt in by label
# get all egress denied, which is what makes the recorded network_disabled=true
# evidence meaningful rather than merely asserted.
resource "kubernetes_network_policy_v1" "academic_offline_validation" {
  count = local.academic_assets_enabled && var.academic_assets.delivery.deny_egress_on_validate ? 1 : 0

  metadata {
    name      = "academic-offline-validation-deny-egress"
    namespace = var.academic_assets.namespace
    labels    = local.academic_common_labels
  }

  spec {
    pod_selector {
      match_labels = {
        "fs2.nebius.ai/offline-validation" = "true"
      }
    }

    policy_types = ["Egress"]
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}
