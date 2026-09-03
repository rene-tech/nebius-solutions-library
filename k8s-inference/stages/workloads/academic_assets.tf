# Tenant-private delivery of licensed academic assets.
#
# The implementation lives in a reusable module so the claim-lifecycle contract
# can be exercised with provider-mocked plan, state and destroy tests without
# standing up the whole workloads stage. See modules/academic-assets/tests.

locals {
  academic_chart_values = {
    enabled   = var.academic_assets.enabled
    namespace = var.academic_assets.namespace
    claim     = var.academic_assets.runtime_claim.name
    mountRoot = var.academic_assets.delivery.mount_root
    assetGid  = var.academic_assets.delivery.asset_gid
    readOnly  = true

    # The renderer needs the exact bindings, not just a claim and a mount root:
    # a subPath mount cannot be generated from those alone.
    runtimeBindings = {
      for key, asset in var.academic_assets.assets : key => {
        modelId             = asset.model_id
        artifactId          = asset.runtime_binding.artifact_id
        sourceSubPath       = asset.runtime_binding.source_sub_path
        consumerPath        = asset.runtime_binding.consumer_path
        mechanism           = asset.runtime_binding.mechanism
        contentIdentityKind = asset.runtime_binding.content_identity_kind
        contentDigestSha256 = asset.runtime_binding.content_digest_sha256
        readOnly            = true
      } if asset.runtime_binding != null
    }
  }
}

module "academic_assets" {
  source = "../../modules/academic-assets"

  academic_assets = var.academic_assets
}

# Publishes what the chart will receive, so the projection is assertable without
# standing up the whole stage. Without this the chart silently kept its disabled
# default even when the root facade enabled the feature.
resource "terraform_data" "academic_assets_contract" {
  input = {
    helm_values = local.academic_chart_values
    delivery = {
      mode                 = var.academic_assets.delivery.mode
      embed_licensed_bytes = var.academic_assets.delivery.embed_licensed_bytes
      general_shared_cache = var.academic_assets.delivery.general_shared_cache
      world_readable       = var.academic_assets.delivery.world_readable
    }
  }
}
