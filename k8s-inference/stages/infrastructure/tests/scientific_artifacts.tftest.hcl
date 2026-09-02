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
    condition = (
      terraform_data.scientific_artifacts_contract[0].input.forbid_deletion == false &&
      terraform_data.scientific_artifacts_contract[0].input.bucket_lifecycle == "disposable"
    )
    error_message = "An enabled store must default to disposable so a teardown is never blocked."
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
      terraform_data.scientific_artifacts_contract[0].input.bind_existing_bucket == true &&
      terraform_data.scientific_artifacts_contract[0].input.bucket_lifecycle == "bound"
    )
    error_message = "Binding a shared artifact plane must reuse its bucket rather than provisioning a duplicate."
  }
}

# The bucket resources cannot be planned here because the stage's exact-target
# precondition reads network data sources that Terraform's mock engine cannot
# express. These runs therefore assert the selection locals that drive each
# bucket's count; test_contract.py pins which resource each local guards and
# that only the retained one carries prevent_destroy.
run "a_retained_bucket_is_protected_from_destroy" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled         = true
      bucket_name     = "fs2-scientific-artifacts-synthetic"
      create_bucket   = true
      forbid_deletion = true
      region          = "us-north1"
    }
  }

  assert {
    condition = (
      local.scientific_artifacts_retained == true &&
      local.scientific_artifacts_disposable == false
    )
    error_message = "forbid_deletion must select the protected bucket resource, not merely a receipt field."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.bucket_lifecycle == "retained"
    error_message = "The receipt must record the retained lifecycle it was planned with."
  }
}

run "a_disposable_bucket_is_destroyed_with_the_run" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled         = true
      bucket_name     = "fs2-scientific-artifacts-synthetic"
      create_bucket   = true
      forbid_deletion = false
      region          = "us-north1"
    }
  }

  assert {
    condition = (
      local.scientific_artifacts_retained == false &&
      local.scientific_artifacts_disposable == true
    )
    error_message = "forbid_deletion = false must select the deletable bucket resource."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.bucket_lifecycle == "disposable"
    error_message = "The receipt must record the deletable lifecycle it was planned with."
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

run "no_default_configuration_selects_the_protected_bucket" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "fs2-scientific-artifacts-synthetic"
      region      = "us-north1"
    }
  }

  assert {
    condition     = local.scientific_artifacts_retained == false
    error_message = "Retention must be an explicit opt-in; no default may block a teardown."
  }
}
