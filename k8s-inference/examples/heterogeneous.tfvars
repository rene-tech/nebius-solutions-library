# Heterogeneous B300/H100 example. Replace only values in this file with the
# exact platforms, presets, project, network, images, and model set offered in
# the target region. `inference-stack preflight` verifies the live provider
# matrix before apply.

deployment = {
  schema_version = 1
  name           = "inference-heterogeneous"

  target = {
    project_id   = "project-yourprojectid"
    project_name = "my-inference-project"
    region       = "eu-north1"
    network = {
      network_name        = "default-network"
      subnet_name         = "default-subnet"
      private_subnet_cidr = "10.0.0.0/16"
    }
    system_update_strategy = { max_surge = 1, max_unavailable = 0 }
  }

  profiles = {
    capacity     = "minimal"
    accelerators = "minimal"
    models       = "full_catalog"
  }

  accelerator_pools = {
    "b300-8x-local" = {
      platform          = "gpu-b300-sxm"
      preset            = "8gpu-192vcpu-2768gb"
      accelerator_class = "nvidia-b300-sxm6-288gb"
      gpus_per_node     = 8
      gpu_memory_gb     = 288
      capacity_type     = "preemptible"
      min_nodes         = 0
      max_nodes         = 1
      driver            = { mode = "managed", preset = "cuda13.0" }
      boot_disk         = { type = "NETWORK_SSD", size_gib = 2048 }
      local_nvme        = true
      local_nvme_mode   = "kubelet-ephemeral"
      # Illustrative plan fixture below the preset's nominal 192 vCPU / 2768
      # GB. fixture:utf8 names the exact bytes hashed by payload_sha256; it does
      # not claim a live measurement. Replace the capacity and the complete
      # evidence record with the target pool's observed allocatable before use.
      schedulable_capacity = {
        cpu_millicores = 188000
        memory_mib     = 2801664
        evidence = {
          pool_id        = "b300-8x-local"
          source         = "fixture:utf8:b300-8x-local"
          captured_at    = "2026-09-03T06:00:00Z"
          payload_sha256 = "cda74692d5d53669c7f4236dcb6cda4bd31a1f832b52e8ee6c0bc00a10d3a480"
        }
      }
    }
    "h100-1x" = {
      platform          = "gpu-h100-sxm"
      preset            = "1gpu-16vcpu-200gb"
      accelerator_class = "nvidia-h100-sxm5-80gb"
      gpus_per_node     = 1
      gpu_memory_gb     = 80
      capacity_type     = "regular"
      min_nodes         = 0
      max_nodes         = 1
      driver            = { mode = "managed", preset = "cuda13.0" }
      local_nvme        = false
      schedulable_capacity = {
        cpu_millicores = 14000
        memory_mib     = 194560
        evidence = {
          pool_id        = "h100-1x"
          source         = "fixture:utf8:h100-1x"
          captured_at    = "2026-09-03T06:00:00Z"
          payload_sha256 = "0aa8d5cb40e63d1c6321af4a4bb4addb1e2a8af6a4868dfd369939c6e7efb784"
        }
      }
    }
  }

  # A small elastic general CPU pool for scientific preprocessing and
  # aggregation. It scales from zero, is preemptible, and is a separate owner
  # from the reference pool below: its own taint, ResourceFlavor and
  # ClusterQueue, and no reference-data filesystem.
  cpu_pools = {
    "general-cpu-8x" = {
      platform      = "cpu-d3"
      preset        = "8vcpu-32gb"
      capacity_type = "preemptible"
      autoscaling   = { min_nodes = 0, max_nodes = 4 }
      schedulable_capacity = {
        cpu_millicores        = 7000
        memory_mib            = 28672
        ephemeral_storage_mib = 114688
      }
      boot_disk = { type = "NETWORK_SSD", size_gib = 160 }
    }
  }

  scheduling = {
    general_cpu = {
      cluster_queue = "general-cpu"
      local_queue   = "general-cpu"
      # One execution namespace, named exactly. v1 binds a class to a single
      # namespace because a consumer keys LocalQueues by bare name and cannot
      # represent the same name twice.
      namespace = "fs2-models"
    }
    # A CPU pool and the reference-data plane both budget cpu and memory, and
    # Kueue drops core requests before admission until this is set. Without it
    # every cpu/memory quota in the cluster is inert, so the facade refuses the
    # combination rather than shipping a quota nothing enforces. This is the
    # measured aggregate schedulable capacity of the pools backing Kueue.
    # Count cpu and memory in Kueue admission, coupled to each accelerator
    # pool: each pool's budget is the measured per-node capacity declared
    # above times its maximum node count. The general CPU pool is not part of
    # it; its ClusterQueue is external, with its own flavor and quota.
    budget_core_resources = true
  }

  storage = {
    reference_data = {
      enabled   = true
      namespace = "fs2-reference-data"
      # Separate from the general pool above, and large enough that one
      # AlphaFold 3 raw-input pod (16 CPU / 64 GiB) fits on a single node. The
      # bulk stager stays at 6 CPU / 24 GiB and is unaffected.
      cpu_pool = {
        platform   = "cpu-d3"
        preset     = "32vcpu-128gb"
        node_count = 1
        schedulable_capacity = {
          cpu_millicores        = 30000
          memory_mib            = 122880
          ephemeral_storage_mib = 114688
        }
      }
      queue = {
        nominal_cpu    = "16"
        nominal_memory = "64Gi"
      }
    }
  }

  models = {
    selection = "explicit"
    enabled   = ["glm-5-2-fp8", "qwen3-8b"]
    pool_overrides = {
      "glm-5-2-fp8" = "b300-8x-local"
      "qwen3-8b"    = "h100-1x"
    }
    scaling = {
      mode = "keda"
      hot  = []
    }
  }

  edge = {
    mode = "internal-only"
    # Offset this tuple when another cluster uses the default 1808x ports.
    port_forward_ports = {
      control_plane  = 28080
      admin_console  = 28081
      operator_proxy = 28082
    }
  }

  applications = {
    control_plane = {
      repository             = "registry.example.invalid/k8s-inference/control-plane"
      digest                 = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      catalog_rollout_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
    admin_console = {
      repository = "registry.example.invalid/k8s-inference/admin-console"
      digest     = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      provenance = {
        source_commit = "1111111111111111111111111111111111111111"
        source_tree   = "2222222222222222222222222222222222222222"
        sbom_sha256   = "3333333333333333333333333333333333333333333333333333333333333333"
        sbom_format   = "cyclonedx-json"
      }
    }
  }
}
