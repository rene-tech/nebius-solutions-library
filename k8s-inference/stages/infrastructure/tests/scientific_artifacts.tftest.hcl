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
  mock_resource "nebius_iam_v1_access_permit" {
    defaults = { id = "accesspermit-syntheticlocal" }
  }
  mock_resource "nebius_iam_v2_access_key" {
    defaults = {
      id               = "accesskey-syntheticlocal"
      resource_version = 1
      status = {
        aws_access_key_id   = "synthetic-access-key"
        secret_reference_id = "synthetic-secret-reference"
      }
    }
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
    tenant_ids = ["tenant-a"]
    credential_generations = {
      tenant-a = {
        active_generation    = 1
        retained_generations = [1]
      }
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
      nebius_iam_v1_service_account.scientific_artifact_tenant,
      nebius_iam_v1_group.scientific_artifact_tenant,
      nebius_iam_v2_access_key.scientific_artifact_tenant,
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
      tenant_ids             = []
      credential_generations = {}
      retention_days = 90
    }
  }

  assert {
    condition = (
      length(nebius_storage_v1_bucket.scientific_artifacts) == 0 &&
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable) == 0 &&
      length(nebius_iam_v1_service_account.scientific_artifacts) == 0 &&
      length(nebius_iam_v1_service_account.scientific_artifact_tenant) == 0 &&
      length(nebius_iam_v1_group.scientific_artifact_tenant) == 0 &&
      length(nebius_iam_v2_access_key.scientific_artifact_tenant) == 0 &&
      length(nebius_iam_v2_access_key.scientific_artifacts) == 0
    )
    error_message = "A disabled scientific artifact store must create no bucket, identity, group or key."
  }
}

# Storage on its own: one disposable bucket, one quarantined legacy identity and
# one exact-prefix provider identity and key per tenant. Nothing about the batch
# controller or academic execution is involved.
run "storage_only_creates_one_disposable_versioned_bucket" {
  command = plan

  plan_options {
    target = [
      nebius_storage_v1_bucket.scientific_artifacts,
      nebius_storage_v1_bucket.scientific_artifacts_disposable,
      nebius_iam_v1_service_account.scientific_artifacts,
      nebius_iam_v1_service_account.scientific_artifact_tenant,
      nebius_iam_v1_group.scientific_artifact_tenant,
      nebius_iam_v1_group_membership.scientific_artifact_tenant,
      nebius_iam_v2_access_key.scientific_artifact_tenant,
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
      length(nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules) == 2 &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[0].paths) == "scientific/v1/tenants/tenant-a/*" &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[0].roles) == "storage.uploader,storage.object-viewer,storage.object-lister" &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[1].paths) == "scientific/v1/*" &&
      join(",", nebius_storage_v1_bucket.scientific_artifacts_disposable[0].bucket_policy.rules[1].roles) == "storage.uploader,storage.object-viewer,storage.object-lister"
    )
    error_message = "Tenant and overlap identities must have only uploader/viewer/lister on their exact canonical prefixes; no delete-capable role is allowed."
  }

  assert {
    condition = (
      length(nebius_iam_v1_service_account.scientific_artifacts) == 1 &&
      length(nebius_iam_v1_service_account.scientific_artifact_tenant) == 1 &&
      length(nebius_iam_v1_group.scientific_artifact_tenant) == 1 &&
      length(nebius_iam_v1_group_membership.scientific_artifact_tenant) == 1 &&
      length(nebius_iam_v2_access_key.scientific_artifact_tenant) == 1 &&
      length(nebius_iam_v2_access_key.scientific_artifacts) == 1
    )
    error_message = "The store must retain the quarantined legacy identity and create one isolated provider identity per tenant."
  }

  assert {
    condition = (
      nebius_iam_v2_access_key.scientific_artifact_tenant["tenant-a"].account.service_account.id ==
      nebius_iam_v1_service_account.scientific_artifact_tenant["tenant-a"].id
    )
    error_message = "The broker key must belong only to the exact tenant provider identity."
  }
}

# The two storage-side rules reclaim incomplete multipart state and empty
# markers. Neither current nor noncurrent exact versions can expire.
run "storage_lifecycle_never_expires_an_exact_artifact_version" {
  command = plan

  plan_options {
    target = [nebius_storage_v1_bucket.scientific_artifacts_disposable]
  }

  assert {
    condition = join(",", [
      for rule in nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules : rule.id
      ]) == join(",", [
      "abort-incomplete-multipart-uploads",
      "remove-expired-delete-markers",
    ])
    error_message = "The bucket must carry only the two reviewed non-content storage-hygiene rules."
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
      nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules[1].expiration.expired_object_delete_marker &&
      alltrue([
        for rule in nebius_storage_v1_bucket.scientific_artifacts_disposable[0].lifecycle_configuration.rules :
        try(rule.noncurrent_version_expiration, null) == null
      ])
    )
    error_message = "Incomplete multipart state may expire, but every noncurrent artifact version must remain retained."
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = { active_generation = 1, retained_generations = [1] }
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
      join(",", nebius_storage_v1_bucket.scientific_artifacts[0].bucket_policy.rules[0].roles) == "storage.uploader,storage.object-viewer,storage.object-lister" &&
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
      join(",", terraform_data.scientific_artifacts_contract[0].input.writer_paths) == "scientific/v1/tenants/tenant-a/*" &&
      join(",", terraform_data.scientific_artifacts_contract[0].input.object_roles) == "storage.uploader,storage.object-viewer,storage.object-lister" &&
      terraform_data.scientific_artifacts_contract[0].input.credential_mode == "TENANT_ISOLATED_BROKER_KEYS"
    )
    error_message = "The store contract must pin each tenant prefix, delete-free object roles and isolated-broker key mode."
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = { active_generation = 1, retained_generations = [1] }
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = { active_generation = 1, retained_generations = [1] }
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = { active_generation = 1, retained_generations = [1] }
      }
      retention_days = 0
    }
  }

  expect_failures = [var.scientific_artifacts]
}

# A generation switch is necessarily a separate apply after the new generation
# has been created.  The reversible source contract never permits that switch
# to revoke a retained predecessor in the same infrastructure-first apply.
run "an_active_generation_switch_cannot_revoke_its_predecessor" {
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = {
          active_generation     = 2
          retained_generations  = [1, 2]
          authorized_generations = [2]
        }
      }
      retention_days = 90
    }
  }

  expect_failures = [var.scientific_artifacts]
}

run "an_active_generation_cannot_skip_an_unprepared_predecessor" {
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = {
          active_generation     = 3
          retained_generations  = [1, 3]
          authorized_generations = [1, 3]
        }
      }
      retention_days = 90
    }
  }

  expect_failures = [var.scientific_artifacts]
}

# Deployment readiness is not provider readiness. Until an independently
# signed observation covers the exact generation's key, prefix, bucket, DB and
# issuer path, even a fully overlapped active switch must fail at input time.
run "an_active_generation_switch_without_external_readiness_is_rejected" {
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
      tenant_ids = ["tenant-a"]
      credential_generations = {
        tenant-a = {
          active_generation      = 2
          retained_generations   = [1, 2]
          authorized_generations = [1, 2]
        }
      }
      retention_days = 90
    }
  }

  expect_failures = [var.scientific_artifacts]
}
