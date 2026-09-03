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
    defaults = { id = "storagebucket-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_service_account" {
    defaults = { id = "serviceaccount-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_group" {
    defaults = { id = "group-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_group_membership" {
    defaults = { id = "groupmembership-syntheticlocal" }
  }
  mock_resource "nebius_iam_v2_access_key" {
    defaults = { id = "accesskey-syntheticlocal" }
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
  project_id    = "project-syntheticlocal"
  source_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id        = "artstore1"
  cluster_name  = "fs2-artifact-store-test"

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

  scientific_artifacts = {
    enabled = true
    lifecycle = {
      retention_mode = "disposable"
    }
    object_storage = {
      bucket_name  = "fs2-artifact-store-test-results"
      max_size_gib = 4096
    }
    retention_days = 90
  }
}

# The store is off by default, so a deployment that does not ask for scientific
# results creates no bucket, no identity and no key at all.
run "disabled_creates_no_store" {
  command = plan

  plan_options {
    target = [
      nebius_storage_v1_bucket.scientific_artifacts,
      nebius_storage_v1_bucket.scientific_artifacts_disposable,
      nebius_iam_v1_service_account.scientific_artifacts,
      nebius_iam_v1_group.scientific_artifacts_writers,
      nebius_iam_v2_access_key.scientific_artifacts,
    ]
  }

  variables {
    scientific_artifacts = {
      enabled = false
      lifecycle = {
        retention_mode = "disposable"
      }
      object_storage = {
        bucket_name  = "disabled-scientific-artifacts.invalid"
        max_size_gib = 4096
      }
      retention_days = 90
    }
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.scientific_artifacts) == 0 &&
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable) == 0 &&
      length(nebius_iam_v1_service_account.scientific_artifacts) == 0 &&
      length(nebius_iam_v1_group.scientific_artifacts_writers) == 0 &&
      length(nebius_iam_v2_access_key.scientific_artifacts) == 0
    )
    error_message = "A disabled scientific artifact store must create no bucket, identity, group or key."
  }
}

# Storage on its own: one disposable bucket, one dedicated identity, one
# bucket-scoped permit and one MysteryBox key. Nothing about the batch
# controller or academic execution is involved.
run "storage_only_creates_one_disposable_versioned_bucket" {
  command = plan

  plan_options {
    target = [
      nebius_storage_v1_bucket.scientific_artifacts,
      nebius_storage_v1_bucket.scientific_artifacts_disposable,
      nebius_iam_v1_service_account.scientific_artifacts,
      nebius_iam_v1_group.scientific_artifacts_writers,
      nebius_iam_v1_group_membership.scientific_artifacts_writer,
      nebius_iam_v2_access_key.scientific_artifacts,
    ]
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.scientific_artifacts) == 0 &&
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable) == 1
    )
    error_message = "The default lifecycle must create exactly the disposable bucket."
  }

  assert {
    condition = (
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].name == "fs2-artifact-store-test-results" &&
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].versioning_policy == "ENABLED" &&
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].max_size_bytes == 4096 * 1024 * 1024 * 1024 &&
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].default_storage_class == "STANDARD"
    )
    error_message = "The result bucket must be the requested versioned, capacity-bounded standard-class bucket."
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules) == 1 &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[0].paths) == "scientific/v1/*" &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[0].roles) == "storage.object-editor"
    )
    error_message = "The writer must hold exactly storage.object-editor on the canonical scientific/v1 prefix and nothing else."
  }

  assert {
    condition = (
      length(nebius_iam_v1_service_account.scientific_artifacts) == 1 &&
      length(nebius_iam_v1_group.scientific_artifacts_writers) == 1 &&
      length(nebius_iam_v1_group_membership.scientific_artifacts_writer) == 1 &&
      nebius_iam_v1_service_account.scientific_artifacts[0].name == "fs2-artifact-store-test-scientific-artifacts" &&
      nebius_iam_v1_group.scientific_artifacts_writers[0].name == "fs2-artifact-store-test-scientific-artifact-writers"
    )
    error_message = "The store must own a dedicated service account and writer group, distinct from every other identity."
  }

  assert {
    condition     = nebius_iam_v2_access_key.scientific_artifacts[0].secret_delivery_mode == "MYSTERY_BOX"
    error_message = "The S3 access key must be delivered only through MysteryBox; an inline secret would enter Terraform state."
  }

  assert {
    condition     = nebius_iam_v2_access_key.scientific_artifacts[0].name == "fs2-artifact-store-test-scientific-artifacts"
    error_message = "The key must be the dedicated result-store key, not a shared one."
  }
}

# The three storage-side rules reclaim waste. None of them expires a current
# object: deleting a live result stays an application decision.
run "storage_lifecycle_reclaims_waste_but_never_a_live_result" {
  command = plan

  plan_options {
    target = [nebius_storage_v1_bucket.scientific_artifacts_disposable]
  }

  assert {
    condition = join(",", [
      for rule in nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules : rule.id
      ]) == join(",", [
      "abort-incomplete-multipart-uploads",
      "expire-noncurrent-versions",
      "remove-expired-delete-markers",
    ])
    error_message = "The bucket must carry exactly the three reviewed storage-hygiene rules."
  }

  assert {
    condition = alltrue([
      for rule in nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules :
      rule.status == "ENABLED"
    ])
    error_message = "Every storage-hygiene rule must be enabled."
  }

  assert {
    condition = (
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules[0].abort_incomplete_multipart_upload.days_after_initiation == 1 &&
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules[1].noncurrent_version_expiration.noncurrent_days == 1 &&
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules[2].expiration.expired_object_delete_marker
    )
    error_message = "Incomplete uploads and noncurrent versions must expire after one day and expired delete markers must be removed."
  }

  assert {
    condition = alltrue([
      for rule in nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules :
      try(rule.expiration.days, null) == null && try(rule.expiration.date, null) == null
    ])
    error_message = "No storage rule may expire a current object; the application owns result deletion."
  }
}

# Retention is a different resource, not a flag on the same one, so Terraform's
# literal-only prevent_destroy can actually protect the retained bucket.
run "retained_storage_uses_the_protected_bucket_resource" {
  command = plan

  plan_options {
    target = [
      nebius_storage_v1_bucket.scientific_artifacts,
      nebius_storage_v1_bucket.scientific_artifacts_disposable,
    ]
  }

  variables {
    scientific_artifacts = {
      enabled = true
      lifecycle = {
        retention_mode = "retain"
      }
      object_storage = {
        bucket_name  = "fs2-artifact-store-test-results"
        max_size_gib = 4096
      }
      retention_days = 365
    }
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.scientific_artifacts) == 1 &&
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable) == 0
    )
    error_message = "Retained results must use the prevent_destroy bucket and never the disposable one."
  }

  assert {
    condition = (
      nebius_storage_v1_bucket.scientific_artifacts[0].versioning_policy == "ENABLED" &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts[0].bucket_policy.rules[0].roles) == "storage.object-editor" &&
      nebius_storage_v1_bucket.scientific_artifacts[0].labels.retention == "durable"
    )
    error_message = "The retained bucket must keep the same versioning and writer scope and be labelled durable."
  }
}

# The reference-data plane keeps its own bucket, its own key and its own paths.
run "results_and_reference_data_never_share_a_bucket" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    reference_data = {
      enabled = true
      lifecycle = {
        retention_mode = "disposable"
      }
      cpu_pool = {
        platform   = "cpu-d3"
        preset     = "8vcpu-32gb"
        node_count = 1
        schedulable_capacity = {
          cpu_millicores        = 7000
          memory_mib            = 28672
          ephemeral_storage_mib = 114688
        }
        boot_disk_type  = "NETWORK_SSD"
        boot_disk_gib   = 160
        max_surge       = 1
        max_unavailable = 0
        drain_timeout   = "15m"
      }
      filesystem = {
        size_gib         = 2048
        type             = "NETWORK_SSD"
        block_size_bytes = 4096
        forbid_deletion  = false
      }
      object_storage = {
        bucket_name  = "fs2-artifact-store-test-results"
        max_size_gib = 2048
      }
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "the_canonical_prefix_and_writer_scope_are_fixed" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract[0].input.object_root == "scientific/v1" &&
      join(",", terraform_data.scientific_artifacts_contract[0].input.writer_paths) == "scientific/v1/*" &&
      terraform_data.scientific_artifacts_contract[0].input.writer_role == "storage.object-editor" &&
      terraform_data.scientific_artifacts_contract[0].input.secret_delivery == "MYSTERY_BOX"
    )
    error_message = "The store contract must pin the canonical prefix, the bucket-scoped object-editor role and MysteryBox delivery."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.retention_days == 90
    error_message = "The application retention window must reach the contract unchanged."
  }
}

run "an_invalid_retention_mode_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled = true
      lifecycle = {
        retention_mode = "forever"
      }
      object_storage = {
        bucket_name  = "fs2-artifact-store-test-results"
        max_size_gib = 4096
      }
      retention_days = 90
    }
  }

  expect_failures = [var.scientific_artifacts]
}

run "an_invalid_bucket_name_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled = true
      lifecycle = {
        retention_mode = "disposable"
      }
      object_storage = {
        bucket_name  = "Not A Bucket"
        max_size_gib = 4096
      }
      retention_days = 90
    }
  }

  expect_failures = [var.scientific_artifacts]
}

run "an_out_of_range_retention_window_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled = true
      lifecycle = {
        retention_mode = "disposable"
      }
      object_storage = {
        bucket_name  = "fs2-artifact-store-test-results"
        max_size_gib = 4096
      }
      retention_days = 0
    }
  }

  expect_failures = [var.scientific_artifacts]
}
