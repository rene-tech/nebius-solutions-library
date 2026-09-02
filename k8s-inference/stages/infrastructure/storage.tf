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

resource "nebius_compute_v1_filesystem" "reference_data" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id        = var.project_id
  name             = "${local.resource_name}-reference-data"
  type             = var.reference_data.filesystem.type
  size_gibibytes   = var.reference_data.filesystem.size_gib
  block_size_bytes = var.reference_data.filesystem.block_size_bytes
  forbid_deletion  = var.reference_data.filesystem.forbid_deletion
  labels = merge(local.common_labels, {
    purpose   = "scientific-reference-data"
    retention = "durable"
  })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_service_account" "reference_data" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-reference-data"
  description = "Least-privilege writer for immutable scientific reference data"
  labels = merge(local.common_labels, {
    purpose   = "reference-data-object-writer"
    retention = "durable"
  })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group" "reference_data_writers" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id = var.project_id
  name      = "${local.resource_name}-reference-data-writers"
  labels = merge(local.common_labels, {
    purpose   = "reference-data-object-write"
    retention = "durable"
  })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group_membership" "reference_data_writer" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id = nebius_iam_v1_group.reference_data_writers[0].id
  member_id = nebius_iam_v1_service_account.reference_data[0].id
}

resource "nebius_storage_v1_bucket" "reference_data" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id             = var.project_id
  name                  = var.reference_data.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.reference_data.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  object_audit_logging  = "ALL"
  labels = merge(local.common_labels, {
    purpose   = "scientific-reference-data"
    retention = "durable"
  })
  bucket_policy = {
    rules = [{
      group_id = nebius_iam_v1_group.reference_data_writers[0].id
      paths    = ["reference-data/*", "inputs/*", "preprocessing/*"]
      roles    = ["storage.editor"]
    }]
  }

  depends_on = [nebius_iam_v1_group_membership.reference_data_writer]
}

resource "nebius_iam_v2_access_key" "reference_data" {
  count = var.reference_data.enabled ? 1 : 0

  parent_id            = var.project_id
  name                 = "${local.resource_name}-reference-data"
  description          = "S3 access key for the private reference-data preprocessing plane"
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose   = "reference-data-object-write"
    retention = "durable"
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.reference_data[0].id
    }
  }

  depends_on = [nebius_storage_v1_bucket.reference_data]
}
