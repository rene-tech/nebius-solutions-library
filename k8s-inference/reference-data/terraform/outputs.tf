output "storage_contract" {
  description = "Non-secret same-region immutable object/shared-filesystem layout."
  value = {
    schema                 = "fs2-serve.nebius.ai/reference-data-storage/v1"
    region                 = var.cluster_region
    object_bucket_name     = local.object_bucket_name
    object_endpoint        = local.object_endpoint
    object_prefix          = local.object_prefix
    shared_filesystem_root = local.filesystem_file_uri
    layout = {
      blobs                 = "${local.object_prefix}/blobs/sha256/<sha256>"
      manifests             = "${local.object_prefix}/manifests/sha256/<sha256>.json"
      filesystem_datasets   = "${local.filesystem_file_uri}/datasets/<bundle>/<revision>/sha256/<tree-sha256>"
      preprocessing_inputs  = "s3://<private-input-bucket>/inputs/sha256/<sha256>.<format>"
      preprocessing_outputs = "s3://<private-output-bucket>/preprocessing/<tenant>/<workload>/requests/sha256/<request-sha256>/results/sha256/<result-manifest-sha256>"
    }
  }
}

output "object_storage_secret_name" {
  description = "Non-secret, immutable credential Secret name derived from the current access-key identity."
  value       = local.credentials_secret
}

output "dynamic_configuration" {
  description = "Secret-free handoff for later root/control-plane integration."
  value = {
    schema               = "fs2-serve.nebius.ai/reference-data-configuration/v1"
    namespace            = var.namespace
    cpu_pool_id          = var.cpu_pool.id
    cpu_pool_name        = var.cpu_pool.name
    cpu_pool_schedulable = var.cpu_pool.schedulable_capacity
    capacity_fit = {
      required  = local.required_capacity
      available = local.total_schedulable_capacity
      status    = "validated"
    }
    local_queue                 = var.queue.local_queue
    cluster_queue               = var.queue.cluster_queue
    resource_flavor             = var.queue.resource_flavor
    tools_config_map            = local.tools_config_map
    object_storage_secret       = local.credentials_secret
    shared_filesystem_host_path = var.shared_filesystem_host_path
    public_msa_default          = false
    public_msa_opt_in_enabled   = var.allow_public_msa_opt_in
    private_object_fqdns        = sort(tolist(var.object_storage_egress_fqdns))
    status_service              = var.status.enabled ? "fs2-reference-data-status.${var.namespace}.svc.cluster.local:8080" : null
    source_catalog_sha256       = filesha256("${path.module}/../source-catalog.json")
    requirements_sha256         = filesha256("${path.module}/../model-requirements.json")
    pipeline = var.pipeline.enabled ? {
      job_name                      = kubernetes_manifest.pipeline[0].manifest.metadata.name
      job_namespace                 = var.namespace
      identity                      = local.pipeline_identity
      state                         = "submitted-suspended-awaiting-kueue-admission"
      bundle_id                     = var.pipeline.bundle_id
      revision                      = local.selected_bundle.revision
      upstream_commit               = local.selected_bundle.upstream.revision
      source_sha256                 = local.selected_bundle.upstream.source_sha256
      image                         = var.pipeline.image
      generation                    = var.pipeline.generation
      resumable                     = true
      checksum_policy               = "source-identity+sha256+tree-sha256"
      immutable_job_contract_sha256 = sha256(jsonencode(local.pipeline_job_contract))
    } : null
  }
}
