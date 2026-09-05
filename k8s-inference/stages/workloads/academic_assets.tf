# Tenant-private delivery of licensed academic assets.
#
# The implementation lives in a reusable module so the claim-lifecycle contract
# can be exercised with provider-mocked plan, state and destroy tests without
# standing up the whole workloads stage. See modules/academic-assets/tests.

locals {
  # An academic asset has two paths on the tenant-private claim when it needs
  # localization. `runtime_binding.source_sub_path` is the operator's source
  # tree (for example the installed PyRosetta tree), while the execution map
  # mounts the immutable, content-addressed generation produced from it. Helm
  # must receive the latter or the published mount contract disagrees with the
  # Job it validates.
  #
  # A model may use the same private mount in more than one stage. Collapse
  # those repetitions before selecting the effective runtime path so BindCraft
  # design and aggregation still describe one binding, not two conflicting
  # ones. If no execution-map mount exists, retain the declared source path;
  # that keeps the academic-assets module useful when scientific batch is off.
  academic_declared_runtime_bindings = {
    for key, asset in var.academic_assets.assets : key => {
      model_id = asset.model_id
      binding  = asset.runtime_binding
    } if asset.runtime_binding != null
  }
  academic_scientific_private_mounts = flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : [
        for mount in try(stage.mounts, []) : {
          model_id   = try(model.model_id, "")
          claim_name = try(mount.claim_name, null)
          mount_path = try(mount.mount_path, "")
          sub_path   = try(mount.sub_path, null)
        } if try(mount.kind, "") == "private" && try(mount.sub_path, null) != null
      ]
    ]
  ])
  academic_runtime_subpaths_by_binding = {
    for key, item in local.academic_declared_runtime_bindings : key => sort(distinct([
      for mount in local.academic_scientific_private_mounts : mount.sub_path
      if(
        mount.model_id == item.model_id &&
        mount.claim_name == var.academic_assets.runtime_claim.name &&
        mount.mount_path == item.binding.consumer_path
      )
    ]))
  }

  academic_chart_values = {
    enabled                 = var.academic_assets.enabled
    namespace               = var.academic_assets.namespace
    claim                   = var.academic_assets.runtime_claim.name
    mountRoot               = var.academic_assets.delivery.mount_root
    assetGid                = var.academic_assets.delivery.asset_gid
    readOnly                = true
    tenantId                = var.academic_assets.tenant_id
    readinessManifestSha256 = var.academic_assets.readiness_manifest_sha256

    # The renderer needs the exact bindings, not just a claim and a mount root:
    # a subPath mount cannot be generated from those alone.
    runtimeBindings = {
      for key, item in local.academic_declared_runtime_bindings : key => {
        modelId    = item.model_id
        artifactId = item.binding.artifact_id
        sourceSubPath = (
          length(local.academic_runtime_subpaths_by_binding[key]) == 1
          ? one(local.academic_runtime_subpaths_by_binding[key])
          : item.binding.source_sub_path
        )
        consumerPath        = item.binding.consumer_path
        mechanism           = item.binding.mechanism
        contentIdentityKind = item.binding.content_identity_kind
        contentDigestSha256 = item.binding.content_digest_sha256

        # The chart's values schema requires these three alongside the digest:
        # a tree-manifest binding must name the algorithm that produced its
        # manifest, and both kinds must carry the consumed size plus the exact
        # source archive they were derived from. They are already present on the
        # variable; not projecting them rendered values the chart rejects.
        contentManifestAlgorithm = item.binding.content_manifest_algorithm
        sizeBytes                = item.binding.size_bytes
        sourceArtifact = item.binding.source_artifact == null ? null : {
          filename   = item.binding.source_artifact.filename
          sha256     = item.binding.source_artifact.sha256
          size_bytes = item.binding.source_artifact.size_bytes
        }
        readOnly = true
      }
    }

    # Academic Jobs run where the claim lives; a claim cannot be mounted from
    # another namespace.
    execution = {
      enabled                   = var.academic_assets.enabled && var.academic_assets.execution.enabled
      namespace                 = var.academic_assets.namespace
      localQueue                = var.academic_assets.execution.local_queue
      clusterQueue              = var.academic_assets.execution.cluster_queue
      referenceDataLocalQueue   = local.academic_cpu_lane_enabled ? local.academic_cpu_local_queue_name : ""
      referenceDataClusterQueue = local.academic_cpu_lane_enabled ? var.reference_data.queue.cluster_queue : ""
      serviceAccount            = var.academic_assets.execution.service_account
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
    helm_values                    = local.academic_chart_values
    runtime_attribution_namespaces = local.runtime_attribution_namespaces
    delivery = {
      mode                 = var.academic_assets.delivery.mode
      embed_licensed_bytes = var.academic_assets.delivery.embed_licensed_bytes
      general_shared_cache = var.academic_assets.delivery.general_shared_cache
      world_readable       = var.academic_assets.delivery.world_readable
    }
  }
}
