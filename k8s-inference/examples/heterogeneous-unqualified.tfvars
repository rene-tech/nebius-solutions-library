# Design-only negative example. The H100, H200, B200, GB300, and RTX PRO pool
# templates intentionally have no provider/region qualification.
# `./inference-stack validate` must reject this file before any cloud plan or
# mutation when run from k8s-inference.

deployment = {
  schema_version = 1
  name           = "inference-heterogeneous-reference"

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
    capacity     = "minimal"
    accelerators = "heterogeneous_reference"
    models       = "minimal"
  }

  models = {
    selection = "profile"
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
