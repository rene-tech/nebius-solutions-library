output "ingestion_target" {
  description = "Non-secret, resource-free handoff passed unchanged to the ingestion Job renderer."
  value = {
    project_id                    = var.project_id
    cluster_region                = var.cluster_region
    cluster_name                  = var.cluster_name
    namespace                     = var.namespace
    local_queue                   = var.local_queue
    service_account               = var.service_account
    filesystem_id                 = var.filesystem_id
    filesystem_size_gib           = var.filesystem_size_gib
    shared_filesystem_host_path   = var.shared_filesystem_host_path
    cache_subpath                 = var.cache_subpath
    cache_root                    = local.cache_root
    node_selector                 = var.node_selector
    node_toleration               = var.node_toleration
    cpu_pool_id                   = var.cpu_pool_id
    cpu_pool_name                 = var.cpu_pool_name
    cpu_pool_label                = local.cpu_pool_label
    source_commit                 = var.source_commit
    reference_plane_source_commit = var.reference_plane_source_commit
    public_source_staging_enabled = var.public_source_staging_enabled
  }

  precondition {
    condition     = var.reference_plane_integrated
    error_message = "Artifact ingestion is disabled until the reference-data plan is integrated."
  }

  precondition {
    condition     = var.public_source_staging_enabled
    error_message = "The integrated reference-data network policy must explicitly allow public source staging."
  }
}
