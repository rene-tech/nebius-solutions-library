# Existing admin revisions reference immutable template digests. Retain their
# reviewed bodies verbatim: re-rendering legacy YAML would change those digests
# and can also reintroduce a second writer. This optional input registers only;
# it never seeds a ModelDeployment, creates a pool, or changes availability.
locals {
  model_controller_retained = jsondecode(
    var.model_controller.retained_registration_file == null ? jsonencode({
      schema           = "fs2-serve.nebius.ai/retained-managed-adoptions/v1"
      modelIds         = []
      qualifications   = {}
      bundles          = []
      requiredPoolRefs = {}
      runtimeSources   = {}
    }) : file(pathexpand(var.model_controller.retained_registration_file))
  )
  model_controller_retained_ids            = try(toset(local.model_controller_retained.modelIds), toset([]))
  model_controller_retained_bundles        = try(local.model_controller_retained.bundles, [])
  model_controller_retained_qualifications = try(local.model_controller_retained.qualifications, {})
  model_controller_retained_runtimes = {
    for model_id, source in try(local.model_controller_retained.runtimeSources, {}) :
    model_id => try(jsondecode(file("${local.fs2_root}/${source.path}")), null)
    if can(regex("^catalog/runtime/(native|deployment-runtimes)/[a-z0-9.-]+\\.json$", source.path))
  }
  model_controller_retained_deployment_runtimes = {
    for model_id, runtime in local.model_controller_retained_runtimes : model_id => runtime
    if try(runtime.schema == "fs2-serve.nebius.ai/deployment-runtime/v1", false)
  }
  model_controller_registered_ids = setunion(
    toset(local.model_controller_dynamic_model_ids), local.model_controller_retained_ids,
  )
  model_controller_retained_shapes_valid = try(
    local.model_controller_retained.schema == "fs2-serve.nebius.ai/retained-managed-adoptions/v1" &&
    toset(keys(local.model_controller_retained)) == toset([
      "schema", "modelIds", "qualifications", "bundles", "requiredPoolRefs", "runtimeSources",
    ]) &&
    length(local.model_controller_retained.modelIds) == length(local.model_controller_retained_ids) &&
    length(local.model_controller_retained_ids) <= 512 &&
    local.model_controller_retained_ids == toset(keys(local.model_controller_retained_qualifications)) &&
    local.model_controller_retained_ids == toset(keys(local.model_controller_retained.requiredPoolRefs)) &&
    local.model_controller_retained_ids == toset(keys(local.model_controller_retained_runtimes)) &&
    local.model_controller_retained_ids == toset([for bundle in local.model_controller_retained_bundles : bundle.modelRef]) &&
    length(local.model_controller_retained_bundles) == length(local.model_controller_retained_ids) &&
    alltrue([
      for bundle in local.model_controller_retained_bundles :
      # jsonencode escapes HTML characters; the controller's canonical JSON
      # does not. Retained Kubernetes manifests here use ASCII wire strings.
      bundle.templateDigest == "sha256:${sha256(replace(replace(replace(jsonencode(bundle.resources), "\\u003c", "<"), "\\u003e", ">"), "\\u0026", "&"))}" &&
      contains(local.model_controller_retained_qualifications[bundle.modelRef].templateDigests, bundle.templateDigest) &&
      contains(values(local.model_controller_retained_qualifications[bundle.modelRef].templateRefs), bundle.templateDigest) &&
      length(bundle.resources) > 0 &&
      alltrue([for resource in bundle.resources :
        contains(local.model_controller_supported_template_gvks, "${resource.apiVersion}/${resource.kind}") &&
        resource.metadata.namespace == local.inventory.namespace
      ])
    ]) &&
    alltrue([
      for model_id, runtime in local.model_controller_retained_runtimes :
      filesha256("${local.fs2_root}/${local.model_controller_retained.runtimeSources[model_id].path}") ==
      local.model_controller_retained.runtimeSources[model_id].sha256 &&
      contains(["fs2-serve.nebius.ai/deployment-runtime/v1", "fs2-serve.nebius.ai/native-catalog-model/v1"], runtime.schema) &&
      runtime.record.model.id == model_id &&
      local.model_controller_retained_qualifications[model_id].modelRef == model_id &&
      contains(local.model_controller_retained_qualifications[model_id].artifactManifestDigests,
      "sha256:${runtime.record.cache.artifact.manifest_digest}") &&
      alltrue([for image in local.model_controller_retained_qualifications[model_id].runtimeImages :
        endswith(image, "@${runtime.record.runtime.image.digest}")
      ])
    ]), false,
  )
  model_controller_retained_pools_valid = try(alltrue(flatten([
    for model_id, pool_refs in local.model_controller_retained.requiredPoolRefs : [
      length(pool_refs) > 0,
      alltrue([for pool_ref in pool_refs :
        contains(keys(local.model_controller_pool_envelope), pool_ref) &&
        contains(local.model_controller_retained_qualifications[model_id].acceleratorClasses,
        local.model_controller_pool_envelope[pool_ref].acceleratorClass) &&
        local.model_controller_retained_qualifications[model_id].maxAcceleratorsPerReplica <=
        local.model_controller_pool_envelope[pool_ref].acceleratorsPerNode
      ]),
    ]
  ])), false)
  model_controller_retained_conflicts_absent = try(
    alltrue([for bundle in local.model_controller_retained_bundles : alltrue([
      for existing in local.model_controller_core_bundles :
      existing.modelRef != bundle.modelRef || jsonencode(existing) == jsonencode(bundle)
    ])]) &&
    alltrue([for model_id, qualification in local.model_controller_retained_qualifications :
      !contains(keys(local.model_controller_core_qualifications), model_id) ||
      try(jsonencode(local.model_controller_core_qualifications[model_id]) == jsonencode(qualification), false)
    ]) &&
    alltrue([for model_id, runtime in local.model_controller_retained_deployment_runtimes :
      !contains(keys(local.selected_deployment_runtime_records), model_id) ||
      try(jsonencode(local.selected_deployment_runtime_records[model_id]) == jsonencode(runtime), false)
    ]) &&
    length(setintersection(local.model_controller_retained_ids, var.model_controller.bootstrap_model_ids)) == 0,
    false,
  )
  model_controller_retained_ownership_valid = alltrue([
    for model_id in local.model_controller_retained_ids :
    !contains(keys(local.model_controller_bootstrap_proposals), model_id) &&
    !contains(keys(local.terraform_owned_model_scalers), model_id) &&
    !contains([for document in values(local.terraform_owned_model_manifests) : document.model_id], model_id) &&
    !contains([for route in local.lean_routes.routes : route.model_id], model_id) &&
    alltrue([for bundle in local.model_controller_retained_bundles :
      bundle.modelRef != model_id || contains(local.selected_runtime_ports, bundle.primaryServicePort)
    ])
  ])
}
