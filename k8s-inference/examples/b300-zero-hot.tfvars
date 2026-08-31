# Kubernetes inference example for the hardware-validated full catalog in
# us-north1. This configuration keeps zero GPU nodes and zero model replicas
# hot; KEDA and the cluster autoscaler add preemptible B300 capacity after
# demand is queued. Run it from k8s-inference with ./inference-stack.

deployment = {
  schema_version = 1
  name           = "inference-b300-zero-hot"

  target = {
    project_id   = "project-yourprojectid"
    project_name = "my-inference-project"
    region       = "us-north1"
    network = {
      network_name        = "default-network"
      subnet_name         = "default-subnet"
      private_subnet_cidr = "10.0.0.0/16"
    }
    system_update_strategy = { max_surge = 1, max_unavailable = 0 }
  }

  profiles = {
    capacity     = "full_catalog"
    accelerators = "full_catalog"
    models       = "full_catalog"
  }

  accelerator_pools = {
    "nebius-b300-preemptible-1x" = {
      platform          = "gpu-b300-sxm"
      preset            = "1gpu-24vcpu-346gb"
      accelerator_class = "nvidia-b300-sxm6-288gb"
      gpus_per_node     = 1
      gpu_memory_gb     = 288
      capacity_type     = "preemptible"
      min_nodes         = 0
      max_nodes         = 6
      driver            = { mode = "managed", preset = "cuda13.0" }
    }
    "nebius-b300-preemptible-8x" = {
      platform          = "gpu-b300-sxm"
      preset            = "8gpu-192vcpu-2768gb"
      accelerator_class = "nvidia-b300-sxm6-288gb"
      gpus_per_node     = 8
      gpu_memory_gb     = 288
      capacity_type     = "preemptible"
      min_nodes         = 0
      max_nodes         = 2
      driver            = { mode = "managed", preset = "cuda13.0" }
      local_nvme        = true
      local_nvme_mode   = "kubelet-ephemeral"
    }
  }

  models = {
    selection = "profile"
    scaling = {
      mode = "keda"
      hot  = []
    }
    cold_start_keepers = true
  }

  edge = {
    mode = "internal-only"
    port_forward_ports = {
      control_plane  = 18080
      admin_console  = 18081
      operator_proxy = 18082
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
