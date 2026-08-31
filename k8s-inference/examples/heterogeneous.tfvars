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
      local_nvme        = true
      local_nvme_mode   = "kubelet-ephemeral"
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
