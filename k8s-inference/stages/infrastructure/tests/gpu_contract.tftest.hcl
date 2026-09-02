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
  mock_resource "nebius_mk8s_v1_cluster" {
    defaults = { id = "mk8scluster-syntheticlocal" }
  }
  mock_resource "nebius_mk8s_v1_node_group" {
    defaults = { id = "mk8snodegroup-syntheticlocal" }
  }
  mock_resource "nebius_compute_v1_filesystem" {
    defaults = { id = "computefilesystem-syntheticlocal" }
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
  mock_resource "nebius_vpc_v1_security_group" {
    defaults = { id = "vpcsecuritygroup-syntheticlocal" }
  }
  mock_resource "nebius_registry_v1_registry" {
    defaults = { id = "registry-syntheticlocal" }
  }
}

variables {
  project_id    = "project-syntheticlocal"
  source_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id        = "gputest1"

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

run "custom_h100_and_b300_standard_pools" {
  command = plan

  plan_options {
    target = [terraform_data.gpu_software_contract]
  }

  variables {
    custom_accelerator_pools = {
      h100-regular = {
        platform          = "gpu-h100-sxm"
        preset            = "1gpu-16vcpu-200gb"
        accelerator_class = "nvidia-h100-sxm5-80gb"
        gpus_per_node     = 1
        capacity_type     = "regular"
        min_nodes         = 1
        max_nodes         = 2
        driver = {
          mode   = "managed"
          preset = "cuda13.0"
        }
      }
      b300-preemptible-local = {
        platform          = "gpu-b300-sxm"
        preset            = "8gpu-192vcpu-2768gb"
        accelerator_class = "nvidia-b300-sxm6-288gb"
        gpus_per_node     = 8
        capacity_type     = "preemptible"
        min_nodes         = 0
        max_nodes         = 2
        local_nvme        = true
        driver = {
          mode   = "managed"
          preset = "cuda13.0"
        }
      }
    }
  }

  assert {
    condition = toset(terraform_data.gpu_software_contract.input.managed_pool_ids) == toset([
      "b300-preemptible-local",
      "h100-regular",
    ])
    error_message = "Arbitrary custom pool IDs must reach the GPU software contract."
  }

  assert {
    condition = (
      local.selected_gpu_pools["h100-regular"].provider.platform == "gpu-h100-sxm" &&
      local.selected_gpu_pools["h100-regular"].provider.preset == "1gpu-16vcpu-200gb" &&
      local.selected_gpu_pools["h100-regular"].capacity.default_mode == "regular" &&
      local.selected_gpu_pools["h100-regular"].features.local_storage.mode == "none"
    )
    error_message = "The regular H100 custom pool must retain its provider and storage contract."
  }

  assert {
    condition = (
      local.selected_gpu_pools["b300-preemptible-local"].provider.platform == "gpu-b300-sxm" &&
      local.selected_gpu_pools["b300-preemptible-local"].provider.preset == "8gpu-192vcpu-2768gb" &&
      local.selected_gpu_pools["b300-preemptible-local"].capacity.default_mode == "preemptible" &&
      local.selected_gpu_pools["b300-preemptible-local"].features.local_storage.mode == "host-local-nvme"
    )
    error_message = "The B300 custom pool must retain preemptible capacity and local NVMe."
  }
}

run "custom_operator_mig_pool" {
  command = plan

  plan_options {
    target = [terraform_data.gpu_software_contract]
  }

  variables {
    custom_accelerator_pools = {
      h100-operator-mig = {
        platform          = "gpu-h100-sxm"
        preset            = "1gpu-16vcpu-200gb"
        accelerator_class = "nvidia-h100-sxm5-80gb"
        gpus_per_node     = 1
        resource_name     = "nvidia.com/mig-discovered-profile"
        capacity_type     = "preemptible"
        driver = {
          mode = "operator"
        }
        mig = {
          strategy = "mixed"
          config   = "live-validated-h100-geometry"
        }
      }
    }
  }

  assert {
    condition = (
      toset(terraform_data.gpu_software_contract.input.operator_pool_ids) == toset(["h100-operator-mig"]) &&
      length(terraform_data.gpu_software_contract.input.managed_pool_ids) == 0 &&
      terraform_data.gpu_software_contract.input.mig_strategy == "mixed"
    )
    error_message = "An operator-owned MIG pool must select only GPU Operator and preserve the declared strategy."
  }

  assert {
    condition = (
      local.selected_gpu_pools["h100-operator-mig"].provider.driver.owner == "gpu-operator" &&
      local.selected_gpu_pools["h100-operator-mig"].provider.driver.preset == null &&
      local.selected_gpu_pools["h100-operator-mig"].accelerator.resource_api.resource_name == "nvidia.com/mig-discovered-profile"
    )
    error_message = "MIG driver ownership and the live-discovered resource name must pass through unchanged."
  }
}

run "two_rack_gb300_nvlink_pool" {
  command = plan

  plan_options {
    target = [
      terraform_data.gpu_software_contract,
      nebius_compute_v1_nvl_instance_group.rack,
    ]
  }

  variables {
    custom_accelerator_pools = {
      gb300-two-rack = {
        platform          = "gpu-gb300"
        preset            = "4gpu-112vcpu-800gb"
        accelerator_class = "nvidia-gb300"
        gpus_per_node     = 4
        host_architecture = "arm64"
        capacity_type     = "regular"
        min_nodes         = 36
        max_nodes         = 36
        driver = {
          mode   = "managed"
          preset = "cuda13.0"
        }
        topology = {
          mode              = "nvlink_rack"
          infiniband_fabric = "fabric-4"
          rack_count        = 2
          nodes_per_rack    = 18
        }
      }
    }
  }

  assert {
    condition = (
      length(nebius_compute_v1_nvl_instance_group.rack) == 2 &&
      length(local.gpu_cluster_pools) == 1 &&
      terraform_data.gpu_software_contract.input.network_operator_enabled
    )
    error_message = "A two-rack GB300 pool must create two rack identities and require the cross-rack fabric stack."
  }
}

run "enabled_reference_data_retained_provider_fixture" {
  command = plan

  plan_options {
    target = [
      nebius_compute_v1_filesystem.reference_data,
      nebius_compute_v1_filesystem.reference_data_disposable,
      nebius_iam_v1_service_account.reference_data,
      nebius_iam_v1_group.reference_data_writers,
      nebius_iam_v1_group_membership.reference_data_writer,
      nebius_iam_v2_access_key.reference_data,
      nebius_storage_v1_bucket.reference_data,
      nebius_storage_v1_bucket.reference_data_disposable,
      nebius_mk8s_v1_node_group.reference_data,
    ]
  }

  variables {
    reference_data = {
      enabled   = true
      lifecycle = { retention_mode = "retain" }
      cpu_pool = {
        platform = "cpu-d3", preset = "8vcpu-32gb", node_count = 1
        schedulable_capacity = {
          cpu_millicores = 7000, memory_mib = 28672, ephemeral_storage_mib = 114688
        }
        boot_disk_type = "NETWORK_SSD", boot_disk_gib = 160
        max_surge      = 1, max_unavailable = 0, drain_timeout = "15m"
      }
      filesystem = {
        size_gib         = 2048, type = "NETWORK_SSD"
        block_size_bytes = 4096, forbid_deletion = true
      }
      object_storage = {
        bucket_name = "fs2-provider-fixture-retained", max_size_gib = 2048
      }
    }
  }

  assert {
    condition = (
      length(nebius_compute_v1_filesystem.reference_data) == 1 &&
      length(nebius_compute_v1_filesystem.reference_data_disposable) == 0 &&
      length(nebius_iam_v1_service_account.reference_data) == 1 &&
      length(nebius_iam_v1_group.reference_data_writers) == 1 &&
      length(nebius_iam_v1_group_membership.reference_data_writer) == 1 &&
      length(nebius_iam_v2_access_key.reference_data) == 1 &&
      length(nebius_storage_v1_bucket.reference_data) == 1 &&
      length(nebius_storage_v1_bucket.reference_data_disposable) == 0 &&
      length(nebius_mk8s_v1_node_group.reference_data) == 1
    )
    error_message = "Retained reference data must use the exact protected provider resource set."
  }
}

run "fresh_empty_reference_storage_apply_acceptance" {
  command = apply

  plan_options {
    target = [
      nebius_compute_v1_filesystem.reference_data_disposable,
      nebius_compute_v1_filesystem.reference_data,
      nebius_iam_v1_service_account.reference_data,
      nebius_iam_v1_group.reference_data_writers,
      nebius_iam_v1_group_membership.reference_data_writer,
      nebius_iam_v2_access_key.reference_data,
      nebius_storage_v1_bucket.reference_data_disposable,
      nebius_storage_v1_bucket.reference_data,
      nebius_mk8s_v1_node_group.reference_data,
    ]
  }

  variables {
    reference_data = {
      enabled   = true
      lifecycle = { retention_mode = "disposable" }
      cpu_pool = {
        platform = "cpu-d3", preset = "8vcpu-32gb", node_count = 1
        schedulable_capacity = {
          cpu_millicores = 7000, memory_mib = 28672, ephemeral_storage_mib = 114688
        }
        boot_disk_type = "NETWORK_SSD", boot_disk_gib = 160
        max_surge      = 1, max_unavailable = 0, drain_timeout = "15m"
      }
      filesystem = {
        size_gib         = 2048, type = "NETWORK_SSD"
        block_size_bytes = 4096, forbid_deletion = false
      }
      object_storage = {
        bucket_name = "fs2-provider-fixture-disposable", max_size_gib = 2048
      }
    }
  }

  assert {
    condition = (
      length(nebius_compute_v1_filesystem.reference_data) == 0 &&
      length(nebius_compute_v1_filesystem.reference_data_disposable) == 1 &&
      length(nebius_iam_v1_service_account.reference_data) == 1 &&
      length(nebius_iam_v1_group.reference_data_writers) == 1 &&
      length(nebius_iam_v1_group_membership.reference_data_writer) == 1 &&
      length(nebius_iam_v2_access_key.reference_data) == 1 &&
      length(nebius_storage_v1_bucket.reference_data) == 0 &&
      length(nebius_storage_v1_bucket.reference_data_disposable) == 1 &&
      length(nebius_mk8s_v1_node_group.reference_data) == 1
    )
    error_message = "Disposable empty-volume acceptance must use only deletable provider resources."
  }

  assert {
    condition = (
      nebius_compute_v1_filesystem.reference_data_disposable[0].forbid_deletion == false &&
      nebius_storage_v1_bucket.reference_data_disposable[0].versioning_policy == "ENABLED"
    )
    error_message = "Fresh acceptance storage must be disposable but keep versioning enabled; teardown is valid only before objects are written."
  }
}
