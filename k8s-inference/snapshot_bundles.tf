# Keep terraform.tfvars small: a qualified checkpoint can be selected by a
# checked-in JSON filename instead of embedding its generated compatibility
# metadata. Relative paths resolve from this solution; inline entries override
# file entries so external bundles remain possible without a code change.
locals {
  snapshot_inputs = {
    serving    = var.deployment.dynamic_models.gpu_snapshots
    scientific = var.deployment.scientific_batch.gpu_snapshots
  }
  snapshot_file_bundles = {
    for kind, settings in local.snapshot_inputs : kind => [
      for filename in settings.bundle_files : jsondecode(file(
        startswith(pathexpand(filename), "/") ? pathexpand(filename) : "${path.module}/${filename}"
      ))
    ]
  }
  normalized_snapshot_settings = {
    for kind, settings in local.snapshot_inputs : kind => {
      cache          = settings.cache
      adopt_existing = settings.adopt_existing
      bundles = merge(
        { for bundle in local.snapshot_file_bundles[kind] : bundle.bundle_id => bundle },
        settings.bundles,
      )
    }
  }
}
