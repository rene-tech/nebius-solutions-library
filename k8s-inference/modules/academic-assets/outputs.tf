# Consumers address one claim regardless of which lifecycle was selected, so the
# identity is coalesced here rather than leaking the retained/disposable split.

locals {
  runtime_claims = concat(
    kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained[*],
    kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable[*],
  )

  legacy_claims = concat(
    kubernetes_persistent_volume_claim_v1.academic_assets_legacy_retained[*],
    kubernetes_persistent_volume_claim_v1.academic_assets_legacy_disposable[*],
  )

  runtime_claim = one(local.runtime_claims)
  legacy_claim  = one(local.legacy_claims)

  # Terraform address of the selected resource, so the adoption helper imports
  # into the resource that actually exists in configuration.
  runtime_address = (
    local.runtime_retained
    ? "kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained[0]"
    : local.runtime_disposable
    ? "kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable[0]"
    : null
  )

  legacy_address = (
    local.legacy_retained
    ? "kubernetes_persistent_volume_claim_v1.academic_assets_legacy_retained[0]"
    : local.legacy_disposable
    ? "kubernetes_persistent_volume_claim_v1.academic_assets_legacy_disposable[0]"
    : null
  )
}

output "academic_assets" {
  description = "Tenant-private academic asset delivery: identities a runtime needs to mount the exact licensed assets."
  value = {
    enabled   = var.academic_assets.enabled
    namespace = one(kubernetes_namespace_v1.academic_assets[*].metadata[0].name)

    runtime_claim = {
      name       = try(local.runtime_claim.metadata[0].name, null)
      uid        = try(local.runtime_claim.metadata[0].uid, null)
      lifecycle  = var.academic_assets.runtime_claim.lifecycle
      retained   = local.runtime_retained
      mount_root = var.academic_assets.delivery.mount_root
      read_only  = true
    }

    legacy_quarantine_claim = {
      enabled   = var.academic_assets.legacy_quarantine_claim.enabled
      namespace = var.academic_assets.legacy_quarantine_claim.namespace
      name      = try(local.legacy_claim.metadata[0].name, null)
      uid       = try(local.legacy_claim.metadata[0].uid, null)
      retained  = local.legacy_retained
      mountable = false
    }

    offline_validation_egress_denied = length(kubernetes_network_policy_v1.academic_offline_validation) > 0

    # A consuming pod reads licensed bytes by joining the asset group; it never
    # needs to run as the staging uid and the bytes are never world-readable.
    consumer_pod_contract = {
      supplemental_groups = [var.academic_assets.delivery.asset_gid]
      mount_path          = var.academic_assets.delivery.mount_root
      read_only           = true
      world_readable      = var.academic_assets.delivery.world_readable
    }

    embeds_licensed_bytes = var.academic_assets.delivery.embed_licensed_bytes
    tenant_id             = var.academic_assets.tenant_id
    institution_id        = var.academic_assets.institution_id

    asset_mounts = {
      for key, asset in var.academic_assets.assets : key => {
        model_id   = asset.model_id
        mount_path = "${var.academic_assets.delivery.mount_root}/${key}"
        source     = asset.relative_path
      }
    }
  }
}

output "managed_addresses" {
  description = "Terraform addresses of the selected claims, for adoption of already-populated storage."
  value = {
    namespace      = local.enabled ? "kubernetes_namespace_v1.academic_assets[0]" : null
    runtime_claim  = local.runtime_address
    legacy_claim   = local.legacy_address
    network_policy = length(kubernetes_network_policy_v1.academic_offline_validation) > 0 ? "kubernetes_network_policy_v1.academic_offline_validation[0]" : null
    module_prefix  = "module.academic_assets"
  }
}
