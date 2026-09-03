locals {
  cache_root     = "/reference-data/${var.cache_subpath}"
  cpu_pool_label = var.node_selector["capacity.fs2.nebius/pool"]
}

# This module deliberately owns no cloud or Kubernetes resource. It validates
# the immutable handoff from the integrated reference-data Terraform plan and
# emits only renderer inputs. Applying it cannot create a PVC, namespace, Job,
# or filesystem.
check "integrated_reference_plane" {
  assert {
    condition     = var.reference_plane_integrated
    error_message = "Artifact ingestion is disabled until the reference-data plan is integrated."
  }

  assert {
    condition     = var.public_source_staging_enabled
    error_message = "The integrated reference-data network policy must explicitly allow public source staging."
  }
}
