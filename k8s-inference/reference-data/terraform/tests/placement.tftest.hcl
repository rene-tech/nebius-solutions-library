# The reference and raw-input stages must be placed and sized only from root
# terraform.tfvars, must stay CPU-only, and must never be left unschedulable.

mock_provider "kubernetes" {}

variables {
  cluster_region        = "eu-north1"
  object_storage_region = "eu-north1"
  object_bucket_name    = "fs2-reference-data-placement"
  object_storage_access = {
    access_key_id       = "PLACEMENT0"
    secret_reference_id = "mysteryboxsecret-placement"
    revision            = 1
  }
  shared_filesystem_host_path = "/mnt/fs2-reference-data/data"
  allow_public_source_staging = true
  cpu_pool = {
    id         = "mk8snodegroup-placement"
    name       = "reference-data"
    platform   = "cpu-d3"
    preset     = "8vcpu-32gb"
    node_count = 1
    capacity   = "regular"
    schedulable_capacity = {
      cpu_millicores        = 7000
      memory_mib            = 28672
      ephemeral_storage_mib = 114688
    }
    node_labels = {
      "workload.fs2.nebius/reference-data" = "true"
      "capacity.fs2.nebius/type"           = "regular"
      "capacity.fs2.nebius/pool"           = "reference-data"
      "storage.fs2.nebius/reference-data"  = "true"
    }
    taint = {
      key    = "workload.fs2.nebius/reference-data"
      value  = "true"
      effect = "NoSchedule"
    }
  }
  status = { enabled = false }
  pipeline = {
    enabled   = true
    bundle_id = "alphafold3-public-databases-v3.0"
    image     = "cr.eu-north1.nebius.cloud/fixture/stager@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "placement_is_rendered_from_tfvars_and_stays_cpu_only" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  assert {
    condition     = terraform_data.region_contract.input.placement_contract.schema == "fs2-serve.nebius.ai/reference-data-placement-contract/v1"
    error_message = "the rendered placement contract must use the published schema."
  }
  assert {
    condition     = terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].resource_class == "cpu"
    error_message = "the reference pool must stay CPU-only."
  }
  assert {
    condition     = terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].node_selector == var.cpu_pool.node_labels
    error_message = "placement must select exactly the tfvars-declared stable pool labels."
  }
  assert {
    condition = (
      terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].tolerations[0].key == var.cpu_pool.taint.key &&
      terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].tolerations[0].value == var.cpu_pool.taint.value &&
      terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].tolerations[0].effect == var.cpu_pool.taint.effect
    )
    error_message = "placement must tolerate exactly the tfvars-declared pool taint."
  }
  assert {
    condition     = !can(terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].accelerator)
    error_message = "the reference pool must never reserve an accelerator."
  }
  assert {
    condition = !contains(
      keys(terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].node_selector),
      "kubernetes.io/hostname",
    )
    error_message = "placement must never pin a node identity."
  }
  assert {
    condition = alltrue([
      for stage in ["staging", "raw-input"] :
      terraform_data.region_contract.input.placement_contract.stages[stage].pool == "reference-cpu"
    ])
    error_message = "both CPU stages must bind the dedicated reference pool."
  }
  assert {
    condition = (
      terraform_data.region_contract.input.placement_contract.stages["raw-input"].defaults.cpu == var.preprocess.cpu &&
      terraform_data.region_contract.input.placement_contract.stages["raw-input"].defaults.memory == var.preprocess.memory &&
      terraform_data.region_contract.input.placement_contract.stages["raw-input"].defaults.ephemeral_storage == var.preprocess.ephemeral_storage &&
      terraform_data.region_contract.input.placement_contract.stages["raw-input"].defaults.threads == var.preprocess.threads
    )
    error_message = "raw-input sizing must come from the tfvars preprocess block."
  }
  assert {
    condition = (
      terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].queue.nominal_cpu == var.queue.nominal_cpu &&
      terraform_data.region_contract.input.placement_contract.pools["reference-cpu"].queue.cluster_queue == var.queue.cluster_queue
    )
    error_message = "the rendered queue contract must mirror the tfvars queue."
  }
}

run "the_stager_consumes_the_rendered_placement_contract" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  assert {
    condition     = contains(terraform_data.region_contract.input.pipeline_command, "--placement")
    error_message = "the staging Job must consume the rendered placement contract."
  }
  assert {
    condition     = contains(terraform_data.region_contract.input.pipeline_command, "/etc/fs2-placement/placement.json")
    error_message = "the staging Job must read the mounted placement path."
  }
  assert {
    condition     = contains(terraform_data.region_contract.input.pipeline_command, var.shared_filesystem_host_path)
    error_message = "the staging Job must publish the canonical host root."
  }
  assert {
    condition = length([
      for mount in terraform_data.region_contract.input.pipeline_pod_template.spec.containers[0].volumeMounts :
      mount if mount.mountPath == "/etc/fs2-placement" && mount.readOnly
    ]) == 1
    error_message = "the placement contract must be mounted read-only."
  }
  assert {
    condition = length([
      for volume in terraform_data.region_contract.input.pipeline_pod_template.spec.volumes :
      volume if volume.name == "placement"
    ]) == 1
    error_message = "the placement ConfigMap must be a pod volume."
  }
  assert {
    condition     = startswith(terraform_data.region_contract.input.placement_config_map, "fs2-reference-data-placement-")
    error_message = "the placement ConfigMap name must be content-addressed."
  }
}

run "the_published_handoff_contract_names_the_actual_published_fields" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  assert {
    condition     = terraform_data.region_contract.input.handoff_contract.schema == "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
    error_message = "the handoff output must name the single published handoff schema."
  }
  assert {
    condition     = terraform_data.region_contract.input.handoff_contract.host_root == var.shared_filesystem_host_path
    error_message = "the handoff output must expose the canonical host root."
  }
  assert {
    condition = (
      contains(terraform_data.region_contract.input.handoff_contract.fields, "content.manifest_sha256") &&
      contains(terraform_data.region_contract.input.handoff_contract.fields, "storage.dataset_sub_path")
    )
    error_message = "the handoff output must name the actual manifest and dataset fields."
  }
  assert {
    condition = (
      !contains(terraform_data.region_contract.input.handoff_contract.fields, "content.published_manifest_sha256") &&
      !contains(terraform_data.region_contract.input.handoff_contract.fields, "storage.source_sub_path")
    )
    error_message = "the handoff output must not publish invented field names."
  }
  assert {
    condition     = terraform_data.region_contract.input.handoff_contract.max_inline_inventory_files == 4096
    error_message = "the bounded inventory limit a consumer may enumerate must be published."
  }
}

run "raw_input_below_the_model_requirement_is_rejected_at_plan_time" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    preprocess = {
      cpu               = "6"
      memory            = "24Gi"
      ephemeral_storage = "32Gi"
      threads           = 6
    }
  }

  expect_failures = [terraform_data.region_contract]
}

run "raw_input_below_the_required_memory_is_rejected_at_plan_time" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    preprocess = {
      cpu               = "16"
      memory            = "24Gi"
      ephemeral_storage = "32Gi"
      threads           = 16
    }
  }

  expect_failures = [terraform_data.region_contract]
}

run "the_data_pipeline_lane_reports_it_cannot_run_on_the_staging_pool" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  assert {
    condition = (
      terraform_data.region_contract.input.raw_input_capacity.required.cpu == "16" &&
      terraform_data.region_contract.input.raw_input_capacity.required.memory == "64Gi"
    )
    error_message = "the AlphaFold3 data-pipeline requirement must be published as 16 CPU / 64Gi."
  }
  assert {
    condition = (
      terraform_data.region_contract.input.raw_input_capacity.declared.cpu == var.preprocess.cpu &&
      terraform_data.region_contract.input.raw_input_capacity.declared.memory == var.preprocess.memory
    )
    error_message = "the declared raw-input sizing must come from tfvars."
  }
  assert {
    condition     = terraform_data.region_contract.input.raw_input_capacity.runnable_on_declared_pool == false
    error_message = "an 8vcpu-32gb reference pool must not advertise a runnable data-pipeline lane."
  }
  assert {
    condition     = terraform_data.region_contract.input.raw_input_capacity.pool.preset == "8vcpu-32gb"
    error_message = "the report must name the pool it evaluated."
  }
}

run "a_fitting_cpu_class_makes_the_data_pipeline_lane_runnable" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    cpu_pool = {
      id         = "mk8snodegroup-placement"
      name       = "reference-data"
      platform   = "cpu-d3"
      preset     = "32vcpu-128gb"
      node_count = 1
      capacity   = "regular"
      schedulable_capacity = {
        cpu_millicores        = 31000
        memory_mib            = 122880
        ephemeral_storage_mib = 460800
      }
      node_labels = {
        "workload.fs2.nebius/reference-data" = "true"
        "capacity.fs2.nebius/type"           = "regular"
        "capacity.fs2.nebius/pool"           = "reference-data"
        "storage.fs2.nebius/reference-data"  = "true"
      }
      taint = {
        key    = "workload.fs2.nebius/reference-data"
        value  = "true"
        effect = "NoSchedule"
      }
    }
    queue = {
      nominal_cpu    = "30"
      nominal_memory = "120Gi"
    }
  }

  assert {
    condition     = terraform_data.region_contract.input.raw_input_capacity.runnable_on_declared_pool == true
    error_message = "a pool sized for the data pipeline must report the lane as runnable."
  }
  assert {
    condition     = terraform_data.region_contract.input.placement_contract.stages["staging"].defaults.cpu == "6"
    error_message = "the bulk stager must stay at its own smaller sizing."
  }
}
