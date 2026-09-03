# Tenant-private delivery of licensed academic assets.
#
# Licensed bytes (AlphaFold 3 parameters, the BindCraft PyRosetta prerequisite)
# live on a tenant-private ReadWriteMany claim and are mounted read-only by
# runtime pods. They are never embedded in a container image and never placed in
# a general shared cache.
#
# Claim lifecycle is a first-class input. Terraform requires prevent_destroy to be
# a constant, so "retained" and "disposable" are separate, mutually exclusive
# resources rather than one resource with a computed flag. A long-lived cluster
# selects retained and cannot discard verified bytes; a throwaway acceptance
# cluster selects disposable and tears down cleanly. Outputs coalesce whichever
# one is active, so consumers never need to know which was chosen.
#
# An already-populated claim is adopted rather than recreated; see
# academic-assets/scripts/adopt-live-resources.sh.

locals {
  enabled = var.academic_assets.enabled

  runtime_retained   = local.enabled && var.academic_assets.runtime_claim.lifecycle == "retained"
  runtime_disposable = local.enabled && var.academic_assets.runtime_claim.lifecycle == "disposable"

  legacy_enabled    = local.enabled && var.academic_assets.legacy_quarantine_claim.enabled
  legacy_retained   = local.legacy_enabled && var.academic_assets.legacy_quarantine_claim.retain
  legacy_disposable = local.legacy_enabled && !var.academic_assets.legacy_quarantine_claim.retain

  common_labels = {
    "app.kubernetes.io/name"       = "academic-assets"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "app.kubernetes.io/managed-by" = "terraform"
    "fs2.nebius.ai/tenant-id"      = var.academic_assets.tenant_id
  }

  runtime_labels = merge(local.common_labels, {
    "fs2.nebius.ai/academic-runtime" = "true"
  })

  legacy_labels = merge(local.common_labels, {
    "fs2.nebius.ai/academic-runtime" = "false"
  })

  runtime_annotations = {
    "fs2.nebius.ai/data-classification"  = "licensed-academic-nonredistributable"
    "fs2.nebius.ai/region"               = var.academic_assets.region
    "fs2.nebius.ai/general-shared-cache" = "false"
    "fs2.nebius.ai/delivery-mode"        = var.academic_assets.delivery.mode
    "fs2.nebius.ai/claim-lifecycle"      = var.academic_assets.runtime_claim.lifecycle
  }

  legacy_annotations = {
    "fs2.nebius.ai/data-classification" = "licensed-academic-nonredistributable"
    "fs2.nebius.ai/region"              = var.academic_assets.region
    "fs2.nebius.ai/runtime-mountable"   = "false"
    "fs2.nebius.ai/retention"           = var.academic_assets.legacy_quarantine_claim.retain ? "retain-rejected-artifact-archive" : "disposable-acceptance-copy"
  }
}

resource "kubernetes_namespace_v1" "academic_assets" {
  count = local.enabled ? 1 : 0

  metadata {
    name = var.academic_assets.namespace
    labels = merge(local.common_labels, {
      "kubernetes.io/metadata.name" = var.academic_assets.namespace
    })
  }

  lifecycle {
    # Adopted from a live namespace; annotations added by other controllers must
    # not cause a destroy and recreate of a namespace holding licensed data.
    ignore_changes = [metadata[0].annotations]
  }
}

# --- runtime claim: retained -------------------------------------------------
# Holds verified licensed bytes on a long-lived cluster. Destroying or replacing
# it would discard content that cannot simply be re-downloaded on demand, so the
# plan fails closed instead.

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_runtime_retained" {
  count = local.runtime_retained ? 1 : 0

  wait_until_bound = false

  metadata {
    name        = var.academic_assets.runtime_claim.name
    namespace   = var.academic_assets.namespace
    labels      = local.runtime_labels
    annotations = local.runtime_annotations
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
    prevent_destroy = true

    # A bound claim reports the provisioner's actual capacity and volume, which
    # can differ from the request without meaning anything changed.
    ignore_changes = [
      metadata[0].annotations,
      spec[0].resources[0].requests,
      spec[0].volume_name,
    ]
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

# --- runtime claim: disposable -----------------------------------------------
# Same shape, no destroy guard, so a throwaway acceptance environment created
# from tfvars can be destroyed cleanly.

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_runtime_disposable" {
  count = local.runtime_disposable ? 1 : 0

  wait_until_bound = false

  metadata {
    name        = var.academic_assets.runtime_claim.name
    namespace   = var.academic_assets.namespace
    labels      = local.runtime_labels
    annotations = local.runtime_annotations
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
    ignore_changes = [
      metadata[0].annotations,
      spec[0].resources[0].requests,
      spec[0].volume_name,
    ]
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

# --- historical quarantine claim ---------------------------------------------
# Declared so no academic storage is left unmanaged. It is never runtime
# mountable. Retention is configurable for the same reason as the runtime claim.

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_legacy_retained" {
  count = local.legacy_retained ? 1 : 0

  wait_until_bound = false

  metadata {
    name        = var.academic_assets.legacy_quarantine_claim.name
    namespace   = var.academic_assets.legacy_quarantine_claim.namespace
    labels      = local.legacy_labels
    annotations = local.legacy_annotations
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

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_legacy_disposable" {
  count = local.legacy_disposable ? 1 : 0

  wait_until_bound = false

  metadata {
    name        = var.academic_assets.legacy_quarantine_claim.name
    namespace   = var.academic_assets.legacy_quarantine_claim.namespace
    labels      = local.legacy_labels
    annotations = local.legacy_annotations
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
    ignore_changes = [
      metadata[0].annotations,
      metadata[0].labels,
      spec[0].resources[0].requests,
      spec[0].volume_name,
    ]
  }
}

# Offline runtime validation must be provably offline. Pods that opt in by label
# get all egress denied, which is what makes recorded network_disabled evidence
# meaningful rather than merely asserted.
resource "kubernetes_network_policy_v1" "academic_offline_validation" {
  count = local.enabled && var.academic_assets.delivery.deny_egress_on_validate ? 1 : 0

  metadata {
    name      = "academic-offline-validation-deny-egress"
    namespace = var.academic_assets.namespace
    labels    = local.common_labels
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

# --- academic scientific execution ------------------------------------------
# Jobs that mount the licensed claim must run in the claim's namespace, so the
# queue and the permissions they need are provisioned there rather than assuming
# the default model namespace can reach the volume.

locals {
  execution_enabled = local.enabled && var.academic_assets.execution.enabled

  academic_local_queue_manifest = local.execution_enabled ? {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata = {
      name      = var.academic_assets.execution.local_queue
      namespace = var.academic_assets.namespace
      labels    = local.common_labels
    }
    spec = {
      clusterQueue = var.academic_assets.execution.cluster_queue
    }
  } : null
}

# The execution contract is useful only if its namespace-local queue actually
# exists.  Keep this beside the academic namespace and claim so enabling the
# feature from tfvars creates the complete admission lane rather than merely
# returning a manifest for some later manual apply.
resource "kubernetes_manifest" "academic_local_queue" {
  for_each = local.execution_enabled ? {
    (var.academic_assets.execution.local_queue) = local.academic_local_queue_manifest
  } : {}

  manifest = each.value

  field_manager {
    force_conflicts = false
    name            = "fs2-academic-assets"
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

resource "kubernetes_service_account_v1" "academic_runner" {
  count = local.execution_enabled ? 1 : 0

  metadata {
    name      = var.academic_assets.execution.service_account
    namespace = var.academic_assets.namespace
    labels    = local.common_labels
  }

  automount_service_account_token = false

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

resource "kubernetes_role_v1" "academic_execution" {
  count = local.execution_enabled ? 1 : 0

  metadata {
    name      = "fs2-academic-execution"
    namespace = var.academic_assets.namespace
    labels    = local.common_labels
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "get", "list", "watch", "delete"]
  }

  rule {
    api_groups = ["jobset.x-k8s.io"]
    resources  = ["jobsets"]
    verbs      = ["create", "get", "list", "watch", "delete"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log"]
    verbs      = ["get", "list", "watch"]
  }

  # Read-only on the claim: enough to resolve it, never to change it.
  rule {
    api_groups = [""]
    resources  = ["persistentvolumeclaims"]
    verbs      = ["get", "list", "watch"]
  }

  # The controller reopens Kueue admission from the namespaced Workload after
  # creating a Job or JobSet. Without this read path successful admission is
  # indistinguishable from a permanently pending academic run.
  rule {
    api_groups = ["kueue.x-k8s.io"]
    resources  = ["workloads"]
    verbs      = ["get", "list", "watch"]
  }

  depends_on = [kubernetes_namespace_v1.academic_assets]
}

resource "kubernetes_role_binding_v1" "academic_execution" {
  count = local.execution_enabled ? 1 : 0

  metadata {
    name      = "fs2-academic-execution"
    namespace = var.academic_assets.namespace
    labels    = local.common_labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = one(kubernetes_role_v1.academic_execution[*].metadata[0].name)
  }

  # The control plane creates the Jobs; they run as the namespace-local runner.
  subject {
    kind      = "ServiceAccount"
    name      = var.academic_assets.execution.controller_service_account
    namespace = var.academic_assets.execution.controller_namespace
  }

  subject {
    kind      = "ServiceAccount"
    name      = var.academic_assets.execution.service_account
    namespace = var.academic_assets.namespace
  }
}
