# Durable PostgreSQL backup plane.
#
# Database backups deliberately do not share the reference-data or scientific
# result stores. The bucket is versioned and retained across cluster teardown;
# the only credential is a path-scoped service-account key whose secret half is
# delivered through MysteryBox. No secret value enters Terraform state.

locals {
  postgresql_backup_root                        = "postgresql/v1"
  postgresql_backup_path_scope                  = "${local.postgresql_backup_root}/*"
  postgresql_backup_writer_role                 = "storage.object-editor"
  postgresql_backup_endpoint                    = "https://storage.${local.selected_target.region}.nebius.cloud"
  postgresql_backup_noncurrent_days             = var.postgresql_backup.retention_days + 7
  postgresql_backup_current_base_backup_days    = var.postgresql_backup.retention_days + 2
  postgresql_backup_noncurrent_base_backup_days = local.postgresql_backup_noncurrent_days
  postgresql_backup_current_wal_days            = var.postgresql_backup.retention_days + 7
  postgresql_backup_noncurrent_wal_days         = local.postgresql_backup_noncurrent_days
  postgresql_backup_required_capacity_gib = ceil((
    var.postgresql_backup.database_volume_size_gib * (
      local.postgresql_backup_current_base_backup_days +
      local.postgresql_backup_noncurrent_base_backup_days
    ) +
    var.postgresql_backup.estimated_daily_wal_gib * (
      local.postgresql_backup_current_wal_days +
      local.postgresql_backup_noncurrent_wal_days
    )
  ) * (100 + var.postgresql_backup.capacity_headroom_percent) / 100)
  postgresql_backup_lifecycle_rules = [
    {
      id                                = "abort-incomplete-multipart-uploads"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = { days_after_initiation = 1 }
      expiration                        = null
      noncurrent_version_expiration     = null
      noncurrent_version_transition     = null
      transition                        = null
    },
    {
      id                                = "expire-noncurrent-versions-after-recovery-window"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = null
      expiration                        = null
      noncurrent_version_expiration = {
        noncurrent_days           = local.postgresql_backup_noncurrent_days
        newer_noncurrent_versions = null
      }
      noncurrent_version_transition = null
      transition                    = null
    },
  ]
  postgresql_capacity_plan_binding = sha256(jsonencode({
    schema                      = "fs2-serve.nebius.ai/sai06-capacity-plan/v1"
    project_id                  = nonsensitive(var.project_id)
    region                      = local.selected_target.region
    effective_system_node_count = local.effective_system_pool.node_count
    postgresql_backup = {
      configured_capacity_gib   = var.postgresql_backup.object_storage.max_size_gib
      schedule                  = var.postgresql_backup.schedule
      retention_days            = var.postgresql_backup.retention_days
      database_volume_size_gib  = var.postgresql_backup.database_volume_size_gib
      estimated_daily_wal_gib   = var.postgresql_backup.estimated_daily_wal_gib
      capacity_headroom_percent = var.postgresql_backup.capacity_headroom_percent
      required_capacity_gib     = var.postgresql_backup.required_capacity_gib
    }
  }))
}

resource "terraform_data" "postgresql_backup_contract" {
  count = var.postgresql_backup.enabled ? 1 : 0

  input = {
    bucket_name                       = var.postgresql_backup.object_storage.bucket_name
    max_size_gib                      = var.postgresql_backup.object_storage.max_size_gib
    retention_mode                    = var.postgresql_backup.lifecycle.retention_mode
    retention_days                    = var.postgresql_backup.retention_days
    database_volume_size_gib          = var.postgresql_backup.database_volume_size_gib
    estimated_daily_wal_gib           = var.postgresql_backup.estimated_daily_wal_gib
    current_base_backup_days          = local.postgresql_backup_current_base_backup_days
    noncurrent_base_backup_days       = local.postgresql_backup_noncurrent_base_backup_days
    current_wal_days                  = local.postgresql_backup_current_wal_days
    noncurrent_wal_days               = local.postgresql_backup_noncurrent_wal_days
    capacity_headroom_percent         = var.postgresql_backup.capacity_headroom_percent
    required_capacity_gib             = var.postgresql_backup.required_capacity_gib
    capacity_cost_review_acknowledged = var.postgresql_backup.capacity_cost_review_acknowledged
    region                            = local.selected_target.region
    object_root                       = local.postgresql_backup_root
    writer_role                       = local.postgresql_backup_writer_role
    writer_paths                      = [local.postgresql_backup_path_scope]
    lifecycle_rules                   = [for rule in local.postgresql_backup_lifecycle_rules : rule.id]
    secret_delivery                   = "MYSTERY_BOX"
  }

  lifecycle {
    precondition {
      condition     = var.postgresql_backup.lifecycle.retention_mode == "retain"
      error_message = "PostgreSQL backups are a retained recovery boundary and cannot use disposable lifecycle semantics."
    }
    precondition {
      condition = (
        (!var.reference_data.enabled || var.postgresql_backup.object_storage.bucket_name != var.reference_data.object_storage.bucket_name) &&
        (!var.scientific_artifacts.enabled || var.postgresql_backup.object_storage.bucket_name != var.scientific_artifacts.object_storage.bucket_name)
      )
      error_message = "PostgreSQL backups must use a dedicated bucket, identity and MysteryBox key."
    }
    precondition {
      condition = (
        var.postgresql_backup.object_storage.max_size_gib >=
        local.postgresql_backup_required_capacity_gib
      )
      error_message = "PostgreSQL backup storage is below the retention-aware base-backup, WAL and headroom requirement."
    }
    precondition {
      condition = try(
        var.sai06_capacity_approval != null &&
        var.sai06_capacity_approval.project_id == nonsensitive(var.project_id) &&
        var.sai06_capacity_approval.region == local.selected_target.region &&
        var.sai06_capacity_approval.plan_binding_sha256 == local.postgresql_capacity_plan_binding &&
        timecmp(var.sai06_capacity_approval.reviewed_at, timeadd(plantimestamp(), "5m")) <= 0 &&
        timecmp(var.sai06_capacity_approval.reviewed_at, timeadd(plantimestamp(), "-24h")) >= 0 &&
        timecmp(var.sai06_capacity_approval.valid_until, plantimestamp()) > 0 &&
        timecmp(var.sai06_capacity_approval.valid_until, timeadd(var.sai06_capacity_approval.reviewed_at, "24h")) <= 0 &&
        var.sai06_capacity_approval.effective_system_node_count == local.effective_system_pool.node_count &&
        var.sai06_capacity_approval.system_node_ceiling >= local.effective_system_pool.node_count &&
        var.sai06_capacity_approval.configured_storage_capacity_bytes == var.postgresql_backup.object_storage.max_size_gib * 1024 * 1024 * 1024 &&
        var.sai06_capacity_approval.projected_storage_usage_bytes == var.sai06_capacity_approval.observed_storage_usage_bytes + var.sai06_capacity_approval.configured_storage_capacity_bytes &&
        var.sai06_capacity_approval.storage_ceiling_bytes >= var.sai06_capacity_approval.projected_storage_usage_bytes,
        false,
      )
      error_message = "PostgreSQL backup/system-pool planning requires a fresh project/region-bound numeric SAI-06 capacity receipt whose node and storage ceilings cover the exact effective demand."
    }
  }

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_service_account" "postgresql_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-postgresql-backup"
  description = "Least-privilege CloudNativePG base-backup and WAL-archive writer"
  labels = merge(local.common_labels, {
    purpose   = "postgresql-backup-object-writer"
    retention = "durable"
  })

  depends_on = [terraform_data.postgresql_backup_contract]
}

resource "nebius_iam_v1_group" "postgresql_backup_writers" {
  count = var.postgresql_backup.enabled ? 1 : 0

  parent_id = var.project_id
  name      = "${local.resource_name}-postgresql-backup-writers"
  labels = merge(local.common_labels, {
    purpose   = "postgresql-backup-object-write"
    retention = "durable"
  })

  depends_on = [terraform_data.postgresql_backup_contract]
}

resource "nebius_iam_v1_group_membership" "postgresql_backup_writer" {
  count = var.postgresql_backup.enabled ? 1 : 0

  parent_id = nebius_iam_v1_group.postgresql_backup_writers[0].id
  member_id = nebius_iam_v1_service_account.postgresql_backup[0].id
}

resource "nebius_storage_v1_bucket" "postgresql_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  parent_id             = var.project_id
  name                  = var.postgresql_backup.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.postgresql_backup.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  labels = merge(local.common_labels, {
    purpose   = "postgresql-base-backups-and-wal"
    retention = "durable"
  })
  bucket_policy = {
    rules = [{
      group_id = nebius_iam_v1_group.postgresql_backup_writers[0].id
      paths    = ["postgresql/v1/*"]
      roles    = ["storage.object-editor"]
    }]
  }
  lifecycle_configuration = {
    rules = local.postgresql_backup_lifecycle_rules
  }

  depends_on = [nebius_iam_v1_group_membership.postgresql_backup_writer]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v2_access_key" "postgresql_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  parent_id            = var.project_id
  name                 = "${local.resource_name}-postgresql-backup"
  description          = "S3 access key for CloudNativePG base backups and WAL archives"
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose   = "postgresql-backup-object-write"
    retention = "durable"
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.postgresql_backup[0].id
    }
  }

  depends_on = [nebius_storage_v1_bucket.postgresql_backup]
}
