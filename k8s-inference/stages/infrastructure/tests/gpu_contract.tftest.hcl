mock_provider "nebius" {}

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
