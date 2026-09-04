# Terraform owns only the shared infrastructure dependencies of a dynamic
# fast-start render. The controller continues to own its holder, init
# containers and serving resources, while the payload PVCs remain in the
# existing model bundle. Compile-cache and residency-receipt claims are derived
# from the reviewed mechanism set so the names mounted by the controller cannot
# drift from the names Terraform provisions.

locals {
  fast_start_compile_cache_claim_rows = flatten([
    for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
      for mechanism, declaration in declarations : {
        model_id         = model_id
        namespace        = try(local.model_controller_primary_deployments[model_id].metadata.namespace, "")
        name             = try(declaration.compileCache.claimName, "")
        size_limit_bytes = try(declaration.compileCache.sizeLimitBytes, 0)
      } if mechanism == "regionalCache"
    ]
  ])
  fast_start_residency_receipt_claim_rows = flatten([
    for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
      for mechanism, declaration in declarations : {
        model_id  = model_id
        namespace = try(declaration.holder.namespace, "")
        name      = try(declaration.holder.receiptClaimName, "")
      } if mechanism == "hostMemoryResidency" && try(declaration.residencyMode, "") != "runtime-sleep-offload"
    ]
  ])
  fast_start_payload_claim_rows = flatten([
    for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
      for mechanism, declaration in declarations : {
        model_id  = model_id
        namespace = try(local.model_controller_primary_deployments[model_id].metadata.namespace, "")
        name      = try(declaration.payloadClaimName, "")
        } if mechanism == "regionalCache" || (
        mechanism == "hostMemoryResidency" &&
        try(declaration.residencyMode, "") != "runtime-sleep-offload"
      )
    ]
  ])
  fast_start_model_payload_claim_keys = toset([
    for document in local.model_documents :
    "${try(document.manifest.metadata.namespace, "")}/${try(document.manifest.metadata.name, "")}"
    if try(document.manifest.kind, "") == "PersistentVolumeClaim"
  ])

  fast_start_compile_cache_claim_groups = {
    for claim in local.fast_start_compile_cache_claim_rows :
    "${claim.namespace}/${claim.name}" => claim...
  }
  fast_start_residency_receipt_claim_groups = {
    for claim in local.fast_start_residency_receipt_claim_rows :
    "${claim.namespace}/${claim.name}" => claim...
  }

  fast_start_compile_cache_claims = {
    for key, declarations in local.fast_start_compile_cache_claim_groups : key => {
      namespace = declarations[0].namespace
      name      = declarations[0].name
      size_gib = max(concat(
        [var.fast_start_claims.compile_cache_min_size_gib],
        [for declaration in declarations : ceil(declaration.size_limit_bytes / 1073741824)],
      )...)
      model_ids = sort(distinct([for declaration in declarations : declaration.model_id]))
    }
  }
  fast_start_residency_receipt_claims = {
    for key, declarations in local.fast_start_residency_receipt_claim_groups : key => {
      namespace = declarations[0].namespace
      name      = declarations[0].name
      size_gib  = var.fast_start_claims.residency_receipt_size_gib
      model_ids = sort(distinct([for declaration in declarations : declaration.model_id]))
    }
  }

  fast_start_managed_compile_cache_claims = (
    var.fast_start_claims.manage ? local.fast_start_compile_cache_claims : {}
  )
  fast_start_managed_residency_receipt_claims = (
    var.fast_start_claims.manage ? local.fast_start_residency_receipt_claims : {}
  )

  fast_start_claim_declarations_valid = try(
    length(local.fast_start_compile_cache_claims) <= 512 &&
    length(local.fast_start_residency_receipt_claims) <= 512 &&
    length(setintersection(
      toset(keys(local.fast_start_compile_cache_claims)),
      toset(keys(local.fast_start_residency_receipt_claims)),
    )) == 0 &&
    length(setintersection(
      setunion(
        toset(keys(local.fast_start_compile_cache_claims)),
        toset(keys(local.fast_start_residency_receipt_claims)),
      ),
      local.fast_start_model_payload_claim_keys,
    )) == 0 &&
    alltrue([
      for claim in local.fast_start_payload_claim_rows :
      contains(local.model_controller_dynamic_model_ids, claim.model_id) &&
      contains(local.fast_start_model_payload_claim_keys, "${claim.namespace}/${claim.name}")
    ]) &&
    alltrue([
      for claim in local.fast_start_compile_cache_claim_rows :
      contains(local.model_controller_dynamic_model_ids, claim.model_id) &&
      claim.namespace == local.model_controller_primary_deployments[claim.model_id].metadata.namespace &&
      length(claim.namespace) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.namespace)) &&
      length(claim.name) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.name)) &&
      floor(claim.size_limit_bytes) == claim.size_limit_bytes &&
      claim.size_limit_bytes >= 1 &&
      claim.size_limit_bytes <= 70368744177664
    ]) &&
    alltrue([
      for claim in local.fast_start_residency_receipt_claim_rows :
      contains(local.model_controller_dynamic_model_ids, claim.model_id) &&
      claim.namespace == local.model_controller_primary_deployments[claim.model_id].metadata.namespace &&
      length(claim.namespace) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.namespace)) &&
      length(claim.name) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.name))
    ]),
    false,
  )
}

resource "kubernetes_persistent_volume_claim_v1" "fast_start_compile_cache" {
  for_each = local.fast_start_managed_compile_cache_claims

  wait_until_bound = false

  metadata {
    name      = each.value.name
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"        = "fast-start-compile-cache"
      "fast-start.fs2.nebius/storage-role" = "compile-cache"
    })
    annotations = {
      "fast-start.fs2.nebius/model-refs" = join(",", each.value.model_ids)
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.fast_start_claims.storage_class
    volume_mode        = "Filesystem"

    resources {
      requests = {
        storage = "${each.value.size_gib}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      spec[0].volume_name,
    ]
  }

  depends_on = [terraform_data.model_controller_contract]
}

resource "kubernetes_persistent_volume_claim_v1" "fast_start_residency_receipt" {
  for_each = local.fast_start_managed_residency_receipt_claims

  wait_until_bound = false

  metadata {
    name      = each.value.name
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"        = "fast-start-residency-receipt"
      "fast-start.fs2.nebius/storage-role" = "residency-receipt"
    })
    annotations = {
      "fast-start.fs2.nebius/model-refs" = join(",", each.value.model_ids)
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.fast_start_claims.storage_class
    volume_mode        = "Filesystem"

    resources {
      requests = {
        storage = "${each.value.size_gib}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      spec[0].volume_name,
    ]
  }

  depends_on = [terraform_data.model_controller_contract]
}
