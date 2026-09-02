# Provider-mocked plan fixture for the scientific artifact store gate.
#
# Each run is scoped to the receipt resource, which validates variables only
# and therefore plans without the target network lookups. Resource shape,
# scoping and creation ordering are proved separately by test_contract.py and
# by the real terraform-graph assertions in test_gpu_bootstrap_graph.py.

mock_provider "nebius" {}

variables {
  project_id    = "project-syntheticlocal"
  source_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id        = "artifact1"

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
}

run "disabled_by_default_plans_no_object_storage" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  assert {
    condition = (
      length(terraform_data.scientific_artifacts_contract) == 0 &&
      local.scientific_artifacts_enabled == false
    )
    error_message = "An unconfigured deployment must plan no bucket, identity, permit, or access key."
  }
}

run "enabled_records_one_regional_versioned_store" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled           = true
      bucket_name       = "fs2-scientific-artifacts-synthetic"
      create_bucket     = true
      forbid_deletion   = true
      versioning_policy = "ENABLED"
      max_size_bytes    = 4398046511104
      region            = "us-north1"
    }
  }

  assert {
    condition = (
      length(terraform_data.scientific_artifacts_contract) == 1 &&
      terraform_data.scientific_artifacts_contract[0].input.create_bucket == true &&
      terraform_data.scientific_artifacts_contract[0].input.bind_existing_bucket == false
    )
    error_message = "An enabled store that owns its bucket must plan exactly one creation receipt."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract[0].input.bucket_name == "fs2-scientific-artifacts-synthetic" &&
      terraform_data.scientific_artifacts_contract[0].input.versioning_policy == "ENABLED" &&
      terraform_data.scientific_artifacts_contract[0].input.region == "us-north1" &&
      terraform_data.scientific_artifacts_contract[0].input.max_size_bytes == 4398046511104
    )
    error_message = "The receipt must record the exact bucket, versioning policy, region and ceiling it was planned for."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.forbid_deletion == true
    error_message = "Results outlive the disposable cluster, so the bucket must default to undeletable."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.endpoint == "https://storage.us-north1.nebius.cloud"
    error_message = "The endpoint must be derived from the target region, never configured independently."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.secret_delivery_mode == "INLINE"
    error_message = "The default delivery mode must return the secret so workloads can project it in one lifecycle."
  }
}

run "binding_an_existing_shared_plane_creates_no_second_bucket" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled       = true
      bucket_name   = "fs2-shared-artifact-plane"
      create_bucket = false
      region        = "us-north1"
    }
  }

  assert {
    condition = (
      length(terraform_data.scientific_artifacts_contract) == 1 &&
      terraform_data.scientific_artifacts_contract[0].input.create_bucket == false &&
      terraform_data.scientific_artifacts_contract[0].input.bind_existing_bucket == true
    )
    error_message = "Binding a shared artifact plane must reuse its bucket rather than provisioning a duplicate."
  }
}

run "a_cross_region_bucket_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "fs2-scientific-artifacts-synthetic"
      region      = "eu-north1"
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "a_created_bucket_must_be_versioned" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled           = true
      bucket_name       = "fs2-scientific-artifacts-synthetic"
      create_bucket     = true
      versioning_policy = "DISABLED"
      region            = "us-north1"
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "an_invalid_bucket_name_is_refused_before_any_plan" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "Not_A_Bucket"
      region      = "us-north1"
    }
  }

  expect_failures = [var.scientific_artifacts]
}

run "a_store_without_a_region_is_refused_before_any_plan" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "fs2-scientific-artifacts-synthetic"
    }
  }

  expect_failures = [var.scientific_artifacts]
}
