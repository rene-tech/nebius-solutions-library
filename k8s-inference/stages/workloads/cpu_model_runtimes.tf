# Existing static CPU services (PDB70) retain their original ownership. Native
# managed CPU Apps use the common controller/KEDA path instead.
resource "terraform_data" "cpu_model_runtime_contract" {
  for_each = local.static_cpu_runtime_records

  input = {
    model_id                 = each.key
    variant_id               = each.value.variant_id
    runtime_image            = var.model_image_overrides[each.key]
    runtime_image_digest     = each.value.record.runtime.image.digest
    artifact_manifest_digest = each.value.record.cache.artifact.manifest_digest
    service                  = each.value.qualification.active_runtime.service
    general_cpu_pool_id      = try(local.general_cpu_runtime_class.pool_resolution.pool_id, null)
    deployment               = local.cpu_runtime_documents[each.key].deployment
    service_manifest         = local.cpu_runtime_documents[each.key].service
  }

  lifecycle {
    precondition {
      condition     = local.cpu_runtime_manifest_validations[each.key]
      error_message = "CPU runtime ${each.key} must be one static digest-pinned Deployment/Service on the exact general-cpu selector and tolerations, with zero GPU resources and its reviewed runtime command, limits, readiness and embedded-database cache layout."
    }
  }
}
