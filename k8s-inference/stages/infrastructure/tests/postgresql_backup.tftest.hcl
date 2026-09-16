mock_provider "nebius" {
  mock_data "nebius_iam_v2_project" {
    defaults = {
      id        = "project-syntheticlocal"
      parent_id = "tenant-syntheticlocal"
      name      = "synthetic-local-project"
      region    = "us-north1"
      status    = { project_state = "ACTIVE" }
    }
  }
  mock_data "nebius_vpc_v1_network" {
    defaults = {
      id     = "vpcnetwork-syntheticlocal"
      name   = "synthetic-network"
      status = { state = "READY" }
    }
  }
  mock_data "nebius_vpc_v1_subnet" {
    defaults = {
      id         = "vpcsubnet-syntheticlocal"
      name       = "synthetic-subnet"
      network_id = "vpcnetwork-syntheticlocal"
      status = {
        state              = "READY"
        ipv4_private_cidrs = ["10.104.0.0/13"]
        ipv4_private_pools = {
          cidrs   = ["10.104.0.0/13"]
          pool_id = "vpcpool-syntheticlocal"
        }
      }
    }
  }
  mock_resource "nebius_storage_v1_bucket" {
    defaults = { id = "storagebucket-postgresql-test" }
  }
  mock_resource "nebius_iam_v1_service_account" {
    defaults = { id = "serviceaccount-postgresql-test" }
  }
  mock_resource "nebius_iam_v1_group" {
    defaults = { id = "group-postgresql-test" }
  }
  mock_resource "nebius_iam_v1_group_membership" {
    defaults = { id = "groupmembership-postgresql-test" }
  }
  mock_resource "nebius_iam_v2_access_key" {
    defaults = { id = "accesskey-postgresql-test" }
  }
  mock_resource "nebius_registry_v1_registry" {
    defaults = { id = "registry-syntheticlocal" }
  }
  mock_resource "nebius_compute_v1_filesystem" {
    defaults = { id = "computefilesystem-syntheticlocal" }
  }
  mock_resource "nebius_mk8s_v1_cluster" {
    defaults = { id = "mk8scluster-syntheticlocal" }
  }
  mock_resource "nebius_mk8s_v1_node_group" {
    defaults = { id = "mk8snodegroup-syntheticlocal" }
  }
  mock_resource "nebius_vpc_v1_security_group" {
    defaults = { id = "vpcsecuritygroup-syntheticlocal" }
  }
}

variables {
  project_id                  = "project-syntheticlocal"
  source_commit               = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id                      = "pgbackup1"
  cluster_name                = "fs2-postgresql-backup-test"
  control_plane_allowed_cidrs = ["192.0.2.1/32"]

  target_binding = {
    project_id          = "project-syntheticlocal"
    project_name        = "synthetic-local-project"
    region              = "us-north1"
    network_name        = "synthetic-network"
    subnet_name         = "synthetic-subnet"
    private_subnet_cidr = "10.104.0.0/13"
    system_update_strategy = {
      max_surge       = 1
      max_unavailable = 0
    }
  }

  public_edge_mode         = "internal-only"
  public_edge_source_cidrs = []

  postgresql_backup = {
    enabled = true
    lifecycle = {
      retention_mode = "retain"
    }
    object_storage = {
      bucket_name  = "fs2-postgresql-backup-test"
      max_size_gib = 6144
    }
    retention_days                    = 30
    database_volume_size_gib          = 100
    estimated_daily_wal_gib           = 32
    capacity_headroom_percent         = 25
    required_capacity_gib             = 5480
    capacity_cost_review_acknowledged = true
  }
}

run "backup_plane_is_versioned_retained_scoped_and_mysterybox_delivered" {
  command = plan

  plan_options {
    target = [
      terraform_data.postgresql_backup_contract,
      nebius_storage_v1_bucket.postgresql_backup,
      nebius_iam_v1_service_account.postgresql_backup,
      nebius_iam_v1_group.postgresql_backup_writers,
      nebius_iam_v1_group_membership.postgresql_backup_writer,
      nebius_iam_v2_access_key.postgresql_backup,
    ]
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.postgresql_backup) == 1 &&
      nebius_storage_v1_bucket.postgresql_backup[0].name == "fs2-postgresql-backup-test" &&
      nebius_storage_v1_bucket.postgresql_backup[0].versioning_policy == "ENABLED" &&
      nebius_storage_v1_bucket.postgresql_backup[0].max_size_bytes == 6144 * 1024 * 1024 * 1024
    )
    error_message = "PostgreSQL requires one dedicated, versioned and retention-aware capacity-bounded backup bucket."
  }

  assert {
    condition = (
      join(",", nebius_storage_v1_bucket.postgresql_backup[0].bucket_policy.rules[0].paths) == "postgresql/v1/*" &&
      join(",", nebius_storage_v1_bucket.postgresql_backup[0].bucket_policy.rules[0].roles) == "storage.object-editor"
    )
    error_message = "The PostgreSQL backup identity must be scoped to storage.object-editor on the canonical prefix only."
  }

  assert {
    condition = (
      length(nebius_iam_v1_service_account.postgresql_backup) == 1 &&
      length(nebius_iam_v1_group.postgresql_backup_writers) == 1 &&
      length(nebius_iam_v1_group_membership.postgresql_backup_writer) == 1 &&
      nebius_iam_v2_access_key.postgresql_backup[0].secret_delivery_mode == "MYSTERY_BOX"
    )
    error_message = "PostgreSQL backups require a dedicated identity and a MysteryBox-delivered key."
  }

  assert {
    condition = (
      terraform_data.postgresql_backup_contract[0].input.retention_mode == "retain" &&
      terraform_data.postgresql_backup_contract[0].input.retention_days == 30 &&
      terraform_data.postgresql_backup_contract[0].input.required_capacity_gib == 5480 &&
      terraform_data.postgresql_backup_contract[0].input.max_size_gib == 6144 &&
      join(",", terraform_data.postgresql_backup_contract[0].input.lifecycle_rules) == "abort-incomplete-multipart-uploads,expire-noncurrent-versions-after-recovery-window" &&
      nebius_storage_v1_bucket.postgresql_backup[0].lifecycle_configuration.rules[1].noncurrent_version_expiration.noncurrent_days == 37
    )
    error_message = "The retained bucket must preserve the 30-day Barman recovery window and reclaim only older non-current versions."
  }
}

run "disposable_postgresql_backup_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.postgresql_backup_contract]
  }

  variables {
    postgresql_backup = {
      enabled = true
      lifecycle = {
        retention_mode = "disposable"
      }
      object_storage = {
        bucket_name  = "fs2-postgresql-backup-test"
        max_size_gib = 6144
      }
      retention_days                    = 30
      database_volume_size_gib          = 100
      estimated_daily_wal_gib           = 32
      capacity_headroom_percent         = 25
      required_capacity_gib             = 5480
      capacity_cost_review_acknowledged = true
    }
  }

  expect_failures = [var.postgresql_backup]
}

run "undersized_postgresql_backup_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.postgresql_backup_contract]
  }

  variables {
    postgresql_backup = {
      enabled = true
      lifecycle = {
        retention_mode = "retain"
      }
      object_storage = {
        bucket_name  = "fs2-postgresql-backup-test"
        max_size_gib = 256
      }
      retention_days                    = 30
      database_volume_size_gib          = 100
      estimated_daily_wal_gib           = 32
      capacity_headroom_percent         = 25
      required_capacity_gib             = 5480
      capacity_cost_review_acknowledged = true
    }
  }

  expect_failures = [var.postgresql_backup]
}
