output "adoption_contract" {
  description = "Non-secret source for the post-apply UID/resourceVersion/spec-hash adoption receipt."
  value = {
    schema          = "fs2-serve.nebius.ai/sai07-custody-adoption/v1"
    cluster_id      = var.cluster_id
    kube_system_uid = var.kube_system_uid
    objects_sha256  = data.external.verified_bundle.result.objects_sha256
    object_count    = tonumber(data.external.verified_bundle.result.object_count)
    objects = {
      for identity, resource in kubernetes_manifest.custody : identity => {
        api_version      = resource.object.apiVersion
        kind             = resource.object.kind
        namespace        = try(resource.object.metadata.namespace, "")
        name             = resource.object.metadata.name
        uid              = resource.object.metadata.uid
        resource_version = resource.object.metadata.resourceVersion
        object_sha256    = sha256(jsonencode(resource.object))
      }
    }
  }
}
