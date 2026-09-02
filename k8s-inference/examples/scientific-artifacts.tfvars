# Scientific artifact store enabled end to end.
#
# This turns on durable result storage for the scientific batch workload:
# Terraform creates a regional bucket and a least-privilege writer identity,
# hands the generated key to the workloads stage as an ephemeral value, and the
# control plane mounts it as a Kubernetes Secret. Without egress_cidrs the
# control plane can presign handles but cannot stream a stored object back to
# verify its digest, so the store reports as configured but not ready.
#
# The bucket is deliberately separate from the reference-data model cache. That
# cache is rebuildable from upstream and is disposable with the run; these are
# tenant results under a retention contract.

deployment = {
  schema_version = 1
  name           = "inference-scientific-artifacts"

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

  models = {
    selection = "profile"
  }

  storage = {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "fs2-scientific-artifacts-example"

      # Disposable with the run, like everything else this stage owns.
      # Set forbid_deletion = true to mark the bucket prevent_destroy when
      # results must survive a teardown; a destroy then fails until it is
      # cleared. Binding an existing bucket with create_bucket = false keeps
      # results outside the run's lifecycle without blocking teardown at all.
      create_bucket = true
      versioning    = "ENABLED"
      max_size_gib  = 4096

      retention_days     = 90
      handle_ttl_seconds = 600
      max_artifact_gib   = 512

      # Object storage reached over TLS from the control plane only. Replace
      # with the exact regional storage prefixes for the target region.
      egress_cidrs = ["203.0.113.0/24"]
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
