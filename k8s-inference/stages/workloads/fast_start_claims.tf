# Terraform owns the finite host-memory holders and the shared infrastructure
# dependencies of a dynamic fast-start render. The controller owns only init
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
  fast_start_host_memory_holder_candidates = flatten([
    for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
      for pool_ref in try(declarations.hostMemoryResidency.poolRefs, []) : {
        key                  = "${model_id}/${pool_ref}"
        model_id             = model_id
        pool_ref             = pool_ref
        namespace            = declarations.hostMemoryResidency.holder.namespace
        candidate_name       = "${declarations.hostMemoryResidency.holder.name}-${pool_ref}"
        image                = var.model_image_overrides[model_id]
        declaration          = declarations.hostMemoryResidency
        node_selector        = local.selected_queue_pools[pool_ref].scheduling.stable_node_labels
        tolerations          = local.selected_queue_pools[pool_ref].scheduling.tolerations
        pod_security_context = try(local.model_controller_primary_deployments[model_id].spec.template.spec.securityContext, {})
      }
      if contains(keys(declarations), "hostMemoryResidency") &&
      declarations.hostMemoryResidency.residencyMode != "runtime-sleep-offload"
    ]
  ])
  # Mirror model_deployment._derived_name() and bounded_label_value() exactly.
  # App UUIDs never enter this identity; the finite model/pool declaration is
  # the only source for the Terraform-owned DaemonSet and its label selector.
  fast_start_host_memory_holder_named = [
    for candidate in local.fast_start_host_memory_holder_candidates : merge(candidate, {
      name = length(candidate.candidate_name) <= 253 ? candidate.candidate_name : format(
        "%s-%s",
        regexreplace(substr(candidate.candidate_name, 0, 240), "[-.]+$", ""),
        substr(sha256(candidate.candidate_name), 0, 12),
      )
    })
  ]
  fast_start_host_memory_holder_rows = [
    for holder in local.fast_start_host_memory_holder_named : merge(holder, {
      label_identity = length(holder.name) <= 63 ? holder.name : format(
        "%s-%s",
        regexreplace(substr(holder.name, 0, 50), "[-.]+$", ""),
        substr(sha256(holder.name), 0, 12),
      )
      agent_name = length("${holder.name}-agent") <= 253 ? "${holder.name}-agent" : format(
        "%s-%s",
        regexreplace(substr("${holder.name}-agent", 0, 240), "[-.]+$", ""),
        substr(sha256("${holder.name}-agent"), 0, 12),
      )
    })
  ]
  fast_start_host_memory_holders = {
    for holder in local.fast_start_host_memory_holder_rows : holder.key => holder
  }
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
      ]) && alltrue([
      for holder in local.fast_start_host_memory_holder_rows :
      length(holder.name) <= 253 &&
      can(regex("^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$", holder.name)) &&
      length(holder.label_identity) <= 63 &&
      can(regex("^[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?$", holder.label_identity)) &&
      length(holder.agent_name) <= 253 &&
      can(regex("^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$", holder.agent_name)) &&
      can(regex("^[^[:space:]@]+@sha256:[a-f0-9]{64}$", holder.image)) &&
      contains(["locked-payload-residency", "mapped-payload-residency"], holder.declaration.residencyMode)
    ]),
    false,
  )
}

# Keep the finite holder set independently plannable and reviewable without
# contacting Kubernetes.  This also prevents a future controller-owned
# DaemonSet path from being introduced without changing the Terraform plan.
resource "terraform_data" "fast_start_host_memory_contract" {
  input = {
    holders = {
      for key, holder in local.fast_start_host_memory_holders : key => {
        name           = holder.name
        namespace      = holder.namespace
        label_identity = holder.label_identity
        pool_ref       = holder.pool_ref
        image          = holder.image
      }
    }
    service_account_name = "fs2-model-runtime"
    network_profile      = "mounted-content"
  }

  lifecycle {
    precondition {
      condition     = local.fast_start_claim_declarations_valid
      error_message = "Fast-start host-memory holder declarations must pass the closed finite contract."
    }
  }
}

# Host-memory holders are finite catalog infrastructure, not App resources.
# One Terraform-owned holder per canonical model/pool publishes receipts that
# arbitrary app-UUID Deployments may consume. The controller therefore needs
# no DaemonSet endpoint or RBAC while the qualified capability remains usable.
resource "kubernetes_config_map_v1" "fast_start_host_memory_agent" {
  for_each = local.fast_start_host_memory_holders

  metadata {
    name      = each.value.agent_name
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"              = "fast-start-host-memory-holder"
      "fast-start.fs2.nebius/model-ref"          = each.value.model_id
      "fast-start.fs2.nebius/host-memory-holder" = each.value.label_identity
      "fs2-serve.nebius.ai/network-profile"      = "mounted-content"
    })
    annotations = {
      "fast-start.fs2.nebius/mechanism"       = "host-memory-residency"
      "fast-start.fs2.nebius/config-digest"   = each.value.declaration.configDigest
      "fs2-serve.nebius.ai/workload-pool-ref" = each.value.pool_ref
    }
  }

  data = {
    "residency_agent.py" = file("${path.module}/../../components/control-plane/src/fs2_serve/residency_agent.py")
  }

  depends_on = [terraform_data.model_controller_contract, terraform_data.fast_start_host_memory_contract]
}

resource "kubernetes_manifest" "fast_start_host_memory_holder" {
  for_each = local.fast_start_host_memory_holders

  manifest = {
    apiVersion = "apps/v1"
    kind       = "DaemonSet"
    metadata = {
      name      = each.value.name
      namespace = each.value.namespace
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"              = "fast-start-host-memory-holder"
        "fast-start.fs2.nebius/model-ref"          = each.value.model_id
        "fast-start.fs2.nebius/host-memory-holder" = each.value.label_identity
      })
      annotations = {
        "fast-start.fs2.nebius/mechanism"       = "host-memory-residency"
        "fast-start.fs2.nebius/config-digest"   = each.value.declaration.configDigest
        "fast-start.fs2.nebius/reserved-memory" = tostring(each.value.declaration.reservedBytes)
        "fs2-serve.nebius.ai/workload-pool-ref" = each.value.pool_ref
      }
    }
    spec = {
      selector = {
        matchLabels = {
          "fast-start.fs2.nebius/host-memory-holder" = each.value.label_identity
        }
      }
      updateStrategy = { type = "RollingUpdate" }
      template = {
        metadata = {
          labels = merge(local.common_labels, {
            "app.kubernetes.io/component"              = "fast-start-host-memory-holder"
            "fast-start.fs2.nebius/model-ref"          = each.value.model_id
            "fast-start.fs2.nebius/host-memory-holder" = each.value.label_identity
            "fs2-serve.nebius.ai/network-profile"      = "mounted-content"
          })
          annotations = {
            "fast-start.fs2.nebius/mechanism"       = "host-memory-residency"
            "fast-start.fs2.nebius/config-digest"   = each.value.declaration.configDigest
            "fast-start.fs2.nebius/reserved-memory" = tostring(each.value.declaration.reservedBytes)
            "fs2-serve.nebius.ai/workload-pool-ref" = each.value.pool_ref
          }
        }
        spec = {
          automountServiceAccountToken = false
          enableServiceLinks           = false
          serviceAccountName           = kubernetes_service_account_v1.model_runtime[0].metadata[0].name
          securityContext              = each.value.pod_security_context
          nodeSelector                 = each.value.node_selector
          tolerations                  = each.value.tolerations
          containers = [{
            name            = "residency-agent"
            image           = each.value.image
            imagePullPolicy = "IfNotPresent"
            command         = ["python3", "/agent/residency_agent.py"]
            env = [
              { name = "PYTHONDONTWRITEBYTECODE", value = "1" },
              { name = "FS2_RESIDENCY_MODEL_REF", value = each.value.model_id },
              { name = "FS2_RESIDENCY_HOLDER_ID", value = each.value.name },
              { name = "FS2_RESIDENCY_MODE", value = each.value.declaration.residencyMode },
              { name = "FS2_RESIDENCY_PAYLOAD_ROOT", value = each.value.declaration.payloadContentPath },
              { name = "FS2_RESIDENCY_PAYLOAD_DIGEST", value = each.value.declaration.payloadDigest },
              { name = "FS2_RESIDENCY_PAYLOAD_BYTES", value = tostring(each.value.declaration.payloadBytes) },
              { name = "FS2_RESIDENCY_RESERVED_BYTES", value = tostring(each.value.declaration.reservedBytes) },
              { name = "FS2_RESIDENCY_CONFIG_DIGEST", value = each.value.declaration.configDigest },
              { name = "FS2_RESIDENCY_RECEIPT_ROOT", value = each.value.declaration.holder.receiptMountPath },
              { name = "FS2_RESIDENCY_REFRESH_SECONDS", value = tostring(max(5, floor(each.value.declaration.receiptMaxAgeSeconds / 3))) },
              { name = "FS2_NODE_NAME", valueFrom = { fieldRef = { fieldPath = "spec.nodeName" } } },
              { name = "FS2_RESIDENCY_HOLDER_INCARNATION", valueFrom = { fieldRef = { fieldPath = "metadata.uid" } } },
            ]
            resources = {
              requests = { cpu = "2", memory = tostring(each.value.declaration.reservedBytes) }
              limits   = { cpu = "8", memory = tostring(each.value.declaration.reservedBytes) }
            }
            securityContext = {
              allowPrivilegeEscalation = false
              capabilities             = { drop = ["ALL"] }
              readOnlyRootFilesystem   = true
              runAsNonRoot             = true
            }
            readinessProbe = {
              exec                = { command = ["python3", "/agent/residency_agent.py", "--check"] }
              initialDelaySeconds = 5
              periodSeconds       = 10
              timeoutSeconds      = 5
              failureThreshold    = 3
            }
            volumeMounts = [
              { name = "agent", mountPath = "/agent", readOnly = true },
              { name = "payload", mountPath = "/${split("/", trimprefix(each.value.declaration.payloadContentPath, "/"))[0]}", readOnly = true },
              { name = "receipt", mountPath = each.value.declaration.holder.receiptMountPath },
              { name = "tmp", mountPath = "/tmp" },
            ]
          }]
          volumes = [
            { name = "agent", configMap = { name = each.value.agent_name, defaultMode = 292 } },
            { name = "payload", persistentVolumeClaim = { claimName = each.value.declaration.payloadClaimName, readOnly = true } },
            { name = "receipt", persistentVolumeClaim = { claimName = each.value.declaration.holder.receiptClaimName } },
            { name = "tmp", emptyDir = {} },
          ]
        }
      }
    }
  }

  depends_on = [
    kubernetes_config_map_v1.fast_start_host_memory_agent,
    kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt,
    kubernetes_service_account_v1.model_runtime,
    terraform_data.model_controller_contract,
  ]
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

  depends_on = [terraform_data.model_controller_contract, terraform_data.fast_start_host_memory_contract]
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
