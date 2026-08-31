resource "nebius_registry_v1_registry" "images" {
  parent_id   = var.project_id
  name        = local.resource_name
  description = "Cluster-regional runtime artifact mirror for ${local.resource_name}"
  labels      = merge(local.common_labels, { purpose = "runtime-artifact-mirror" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_compute_v1_filesystem" "cache" {
  parent_id        = var.project_id
  name             = "${local.resource_name}-cache"
  type             = local.effective_shared_cache.type
  size_gibibytes   = local.effective_shared_cache.size_gib
  block_size_bytes = local.effective_shared_cache.block_size_bytes
  forbid_deletion  = local.effective_shared_cache.forbid_deletion
  labels           = merge(local.common_labels, { purpose = "model-cache" })

  depends_on = [terraform_data.target_contract]
}
