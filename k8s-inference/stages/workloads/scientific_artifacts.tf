# Scientific result artifact store, workload side.
#
# The infrastructure stage hands over three non-secret values: the S3 access-key
# ID, the opaque MysteryBox reference, and a revision. The S3 secret itself is
# resolved here as an ephemeral value and written with the provider's write-only
# argument, so it exists only for the duration of the apply. It is never in this
# stage's state, in a plan file, in generated tfvars, in a Helm release value or
# in any output.
#
# The Secret name is stable on purpose. Workers never mount it: they receive
# short-lived signed handles from the control plane, which is the only consumer
# of the key. Rotating the key changes the revision below, which rewrites the
# write-only data and moves the pod annotation, so the control plane restarts
# and picks the new credential up without the annotation ever carrying a secret.

locals {
  scientific_artifacts_enabled        = var.scientific_artifacts.enabled
  scientific_artifacts_secret_name    = "fs2-serve-artifact-store"
  scientific_artifacts_secret_key     = "credentials.json"
  scientific_runtime_cache_claim_name = "fs2-scientific-runtime-cache"
  scientific_runtime_cache_mount_path = "/cache"
  scientific_runtime_cache_mounts = flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : [
        for mount in try(stage.mounts, []) : {
          model_id   = try(model.model_id, "")
          stage_id   = try(stage.stage_id, "")
          name       = try(mount.name, "")
          claim_name = try(mount.claim_name, null)
          host_path  = try(mount.host_path, null)
          mount_path = try(mount.mount_path, "")
          sub_path   = try(mount.sub_path, null)
          read_only  = try(mount.read_only, null)
        } if try(mount.kind, "") == "runtime-cache"
      ]
    ]
  ])
  # A runtime-cache mount is model-only, but the RWX claim's provider-owned
  # root is not writable by the unprivileged model UID. Derive each exact
  # first-level cache boundary and its owner from the same execution-map stage
  # that the controller renders. Terraform prepares those boundaries once;
  # kubelet never performs a recursive fsGroup rewrite on every cold start.
  scientific_runtime_cache_consumers = flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : {
        model_id      = try(model.model_id, "")
        stage_id      = try(stage.stage_id, "")
        workspace_uid = try(stage.workspace_uid, null)
        workspace_gid = try(stage.workspace_gid, null)
        cache_paths = sort(distinct([
          for value in values(try(stage.environment, {})) : value
          if try(startswith(value, "${local.scientific_runtime_cache_mount_path}/"), false)
        ]))
        } if length([
          for mount in try(stage.mounts, []) : mount
          if try(mount.kind, "") == "runtime-cache"
      ]) == 1
    ]
  ])
  scientific_runtime_cache_directory_claims = flatten([
    for consumer in local.scientific_runtime_cache_consumers : [
      for name in distinct([
        for path in consumer.cache_paths : split("/", path)[2]
        ]) : {
        name     = name
        uid      = consumer.workspace_uid
        gid      = consumer.workspace_gid
        model_id = consumer.model_id
        stage_id = consumer.stage_id
      }
    ]
  ])
  scientific_runtime_cache_directory_claims_by_name = {
    for claim in local.scientific_runtime_cache_directory_claims : claim.name => claim...
  }
  scientific_runtime_cache_directories = [
    for name in sort(keys(local.scientific_runtime_cache_directory_claims_by_name)) : {
      name = name
      uid  = local.scientific_runtime_cache_directory_claims_by_name[name][0].uid
      gid  = local.scientific_runtime_cache_directory_claims_by_name[name][0].gid
      mode = "2770"
    }
  ]
  scientific_runtime_cache_ownership_contract = {
    schema      = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v1"
    root        = local.scientific_runtime_cache_mount_path
    directories = local.scientific_runtime_cache_directories
  }
  scientific_runtime_cache_ownership_sha256 = sha256(jsonencode(
    local.scientific_runtime_cache_ownership_contract
  ))
  scientific_runtime_cache_bootstrap_sha256 = sha256(jsonencode({
    ownership_sha256  = local.scientific_runtime_cache_ownership_sha256
    program_sha256    = filesha256("${path.module}/scripts/scientific_runtime_cache_bootstrap.py")
    runtime_image_ref = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
  }))
  # The rollout identity must move whenever the mounted credential could differ.
  # The cloud key's resource_version restarts at zero when the key is replaced,
  # so a revision derived from it alone repeats after a rotation and leaves the
  # stale secret mounted. Cover the key's own identity instead, and keep the
  # operator's explicit generation as the leading term so a deliberate rotation
  # is always an increase.
  scientific_artifacts_credential_identity = local.scientific_artifacts_enabled ? join("|", [
    var.scientific_artifacts.object_storage_access.key_id,
    var.scientific_artifacts.object_storage_access.access_key_id,
    var.scientific_artifacts.object_storage_access.secret_reference_id,
    tostring(var.scientific_artifacts.object_storage_access.resource_version),
  ]) : ""
  scientific_artifacts_revision = local.scientific_artifacts_enabled ? (
    var.scientific_artifacts.credential_generation * 16777216 +
    parseint(substr(sha256(local.scientific_artifacts_credential_identity), 0, 6), 16)
  ) : 0

  # Zero-or-one comprehension so the disabled case yields an empty map rather
  # than an object Terraform cannot unify with the enabled one.
  scientific_artifacts_overrides = merge([
    for _ in range(local.scientific_artifacts_enabled ? 1 : 0) : {
      scientificArtifacts = {
        enabled          = true
        endpoint         = var.scientific_artifacts.storage_contract.object_storage.endpoint
        bucket           = var.scientific_artifacts.storage_contract.object_storage.name
        region           = var.scientific_artifacts.storage_contract.region
        addressingStyle  = var.scientific_artifacts.storage_contract.object_storage.addressing_style
        verifyTls        = var.scientific_artifacts.storage_contract.object_storage.verify_tls
        handleTtlSeconds = var.scientific_artifacts.handle_ttl_seconds
        maxBytes         = var.scientific_artifacts.max_artifact_bytes
        retentionSeconds = var.scientific_artifacts.retention_days * 86400
        mediaTypes       = sort(var.scientific_artifacts.media_types)
        egressCidrs      = sort(var.scientific_artifacts.egress_cidrs)
      }
      secrets = {
        artifactStore = {
          name = local.scientific_artifacts_secret_name
          key  = local.scientific_artifacts_secret_key
        }
      }
      networkPolicy = {
        artifactStoreCidrs = sort(var.scientific_artifacts.egress_cidrs)
      }
      # Non-secret rollout trigger. It carries the credential revision, never
      # the credential, so a rotation restarts the control plane deterministically.
      podAnnotations = {
        "fs2.nebius.ai/artifact-store-credential-revision" = tostring(local.scientific_artifacts_revision)
      }
    }
  ]...)

  scientific_batch_overrides = {
    scientificBatch = {
      enabled                         = var.scientific_batch.enabled
      writesEnabled                   = var.scientific_batch.writes_enabled
      namespace                       = var.scientific_batch.namespace
      kubernetesApiUrl                = "https://kubernetes.default.svc"
      schedulingContractConfigMapName = local.scheduling_contract_ref.config_map_name
      schedulingContractNamespace     = local.scheduling_contract_ref.namespace
      schedulingContractKey           = local.scheduling_contract_ref.key
      schedulingContractSchema        = local.scheduling_contract_ref.schema
      schedulingContractSha256        = local.scheduling_contract_ref.sha256
      executionMapConfigMapName       = "fs2-${var.run_id}-scientific-execution"
      executionMapKey                 = "execution-map.json"
      executionMap                    = var.scientific_batch.execution_map
      workers                         = var.scientific_batch.workers
      pollSeconds                     = var.scientific_batch.poll_seconds
      leaseSeconds                    = var.scientific_batch.lease_seconds
      apiTimeoutSeconds               = var.scientific_batch.api_timeout_seconds
      tokenExpirationSeconds          = var.scientific_batch.token_expiration_seconds
    }
  }

  scientific_chart_overrides = merge(
    local.scientific_artifacts_overrides,
    local.scientific_batch_overrides,
  )
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  secret_id = var.scientific_artifacts.object_storage_access.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "scientific_artifact_store" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = local.scientific_artifacts_secret_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "scientific-artifact-store"
    })
    annotations = {
      # Non-secret. It exists so an operator can tell which key generation the
      # cluster currently holds without reading the Secret's data.
      "fs2.nebius.ai/artifact-store-credential-revision"   = tostring(local.scientific_artifacts_revision)
      "fs2.nebius.ai/artifact-store-credential-generation" = tostring(var.scientific_artifacts.credential_generation)
      "fs2.nebius.ai/artifact-store-access-key-id"         = var.scientific_artifacts.object_storage_access.access_key_id
    }
  }

  type = "Opaque"

  # data_wo keeps the value out of state entirely; plain `data` would persist it.
  data_wo = {
    (local.scientific_artifacts_secret_key) = jsonencode({
      access_key_id     = var.scientific_artifacts.object_storage_access.access_key_id
      secret_access_key = ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifacts[0].data.string_value
    })
  }
  # Monotonic with the cloud-side key version, so rotating the access key is the
  # only thing that rewrites the Secret.
  data_wo_revision = local.scientific_artifacts_revision

  depends_on = [terraform_data.cluster_contract]
}

# Disposable derived runtime state only: compiled kernels and framework cache
# entries. Immutable model artifacts and tenant inputs never use this claim.
# The stable name is part of the reviewed execution-map contract, while size
# and storage class remain ordinary terraform.tfvars settings.
resource "kubernetes_persistent_volume_claim_v1" "scientific_runtime_cache" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  wait_until_bound = false

  metadata {
    name      = local.scientific_runtime_cache_claim_name
    namespace = var.scientific_batch.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"        = "scientific-runtime-cache"
      "fast-start.fs2.nebius/storage-role" = "compile-cache"
    })
    annotations = {
      "fs2.nebius.ai/data-classification" = "disposable-derived-cache"
      "fs2.nebius.ai/mount-path"          = local.scientific_runtime_cache_mount_path
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.scientific_batch.runtime_cache.storage_class_name
    volume_mode        = "Filesystem"

    resources {
      requests = {
        storage = "${var.scientific_batch.runtime_cache.size_gib}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      spec[0].volume_name,
    ]
  }

  depends_on = [terraform_data.cluster_contract]
}

# Prepare only the model-owned boundaries declared above. The root-capable
# container sees no credential, service-account token, network requirement or
# other writable volume. Its checked-in program refuses nested/traversing names
# and applies ownership non-recursively, so existing compiled entries remain
# untouched across Terraform updates.
resource "kubernetes_job_v1" "scientific_runtime_cache_bootstrap" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  metadata {
    name      = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_bootstrap_sha256, 0, 12)}"
    namespace = var.scientific_batch.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
    })
    annotations = {
      "fs2.nebius.ai/runtime-cache-ownership-sha256" = local.scientific_runtime_cache_ownership_sha256
      "fs2.nebius.ai/runtime-cache-bootstrap-sha256" = local.scientific_runtime_cache_bootstrap_sha256
    }
  }

  spec {
    backoff_limit           = 3
    active_deadline_seconds = 600

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
        })
      }

      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector = {
          "workload.fs2.nebius/system"      = "true"
          "capacity.fs2.nebius/pool"        = "system"
          "storage.fs2.nebius/shared-cache" = "true"
        }

        security_context {
          run_as_non_root = false
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name  = "prepare"
          image = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = [
            "python",
            "-c",
            file("${path.module}/scripts/scientific_runtime_cache_bootstrap.py"),
          ]

          env {
            name  = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
            value = jsonencode(local.scientific_runtime_cache_ownership_contract)
          }

          volume_mount {
            name       = "runtime-cache"
            mount_path = local.scientific_runtime_cache_mount_path
            read_only  = false
          }

          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = false
            run_as_user                = 0
            run_as_group               = 0
            capabilities {
              drop = ["ALL"]
              # After chowning a boundary to its model GID, Linux requires
              # CAP_FSETID to retain setgid when the process is not a member
              # of that GID. Without it chmod(02770) silently becomes 0770.
              add = [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
              ]
            }
          }
        }

        volume {
          name = "runtime-cache"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.scientific_runtime_cache[0].metadata[0].name
            read_only  = false
          }
        }
      }
    }
  }

  wait_for_completion = true
  timeouts { create = "15m" }

  lifecycle { create_before_destroy = true }

  depends_on = [
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache,
    terraform_data.scientific_artifacts_contract,
  ]
}

# Publishes exactly what the control-plane chart receives, so the projection is
# assertable without standing up the whole stage. Everything here is non-secret.
resource "terraform_data" "scientific_artifacts_contract" {
  input = {
    enabled     = local.scientific_artifacts_enabled
    secret_name = local.scientific_artifacts_secret_name
    secret_key  = local.scientific_artifacts_secret_key
    namespace   = "fs2-system"
    bucket_name = try(var.scientific_artifacts.storage_contract.object_storage.name, null)
    object_key  = try(var.scientific_artifacts.storage_contract.layout.object_key, null)
    # Derived from the cloud-side key version alone, so a rotation is visible
    # without ever recording the secret.
    credential_revision   = local.scientific_artifacts_revision
    credential_generation = var.scientific_artifacts.credential_generation
    # A digest of the non-secret key identity, so the receipt shows that a
    # replaced key really does move the rollout identity.
    credential_identity_sha256 = local.scientific_artifacts_enabled ? sha256(local.scientific_artifacts_credential_identity) : null
    chart_values               = local.scientific_chart_overrides
    batch = {
      enabled        = var.scientific_batch.enabled
      writes_enabled = var.scientific_batch.writes_enabled
      namespace      = var.scientific_batch.namespace
      runtime_cache = {
        enabled            = var.scientific_batch.runtime_cache.enabled
        claim_name         = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_claim_name : null
        mount_path         = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_mount_path : null
        storage_class_name = var.scientific_batch.runtime_cache.storage_class_name
        size_gib           = var.scientific_batch.runtime_cache.size_gib
        ownership = var.scientific_batch.runtime_cache.enabled ? {
          bootstrap_job    = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_bootstrap_sha256, 0, 12)}"
          bootstrap_sha256 = local.scientific_runtime_cache_bootstrap_sha256
          contract_sha256  = local.scientific_runtime_cache_ownership_sha256
          directories      = local.scientific_runtime_cache_directories
        } : null
        consumers = sort([
          for mount in local.scientific_runtime_cache_mounts : "${mount.model_id}/${mount.stage_id}"
        ])
      }
    }
  }

  lifecycle {
    precondition {
      condition     = !var.scientific_batch.enabled || var.scientific_artifacts.enabled
      error_message = "staged scientific batch execution requires the dedicated artifact store; a batch cannot commit an immutable result manifest without it."
    }
    precondition {
      condition     = !var.scientific_batch.writes_enabled || var.scientific_batch.enabled
      error_message = "scientific batch Kubernetes writes require the batch controller gate."
    }
    precondition {
      condition = (
        !var.scientific_batch.enabled || (
          (length(local.scientific_runtime_cache_mounts) == 0 || var.scientific_batch.runtime_cache.enabled) &&
          (!var.scientific_batch.runtime_cache.enabled || length(local.scientific_runtime_cache_mounts) > 0) &&
          alltrue([
            for mount in local.scientific_runtime_cache_mounts :
            mount.claim_name == local.scientific_runtime_cache_claim_name &&
            mount.host_path == null &&
            mount.mount_path == local.scientific_runtime_cache_mount_path &&
            mount.sub_path == null &&
            mount.read_only == false
          ])
        )
      )
      error_message = "A scientific runtime cache must be enabled exactly when the execution map consumes it, and every consumer must use the Terraform-owned writable fs2-scientific-runtime-cache claim at /cache."
    }
    precondition {
      condition = (
        !var.scientific_batch.runtime_cache.enabled || (
          length(local.scientific_runtime_cache_consumers) > 0 &&
          alltrue([
            for consumer in local.scientific_runtime_cache_consumers :
            length(consumer.cache_paths) > 0 &&
            length(distinct([
              for path in consumer.cache_paths : split("/", path)[2]
            ])) == 1
          ]) &&
          alltrue([
            for claim in local.scientific_runtime_cache_directory_claims :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.name)) &&
            try(claim.uid >= 1 && claim.uid <= 2147483647, false) &&
            try(claim.gid >= 1 && claim.gid <= 2147483647, false)
          ]) &&
          alltrue([
            for claims in values(local.scientific_runtime_cache_directory_claims_by_name) :
            length(distinct([for claim in claims : claim.uid])) == 1 &&
            length(distinct([for claim in claims : claim.gid])) == 1
          ])
        )
      )
      error_message = "Every runtime-cache stage must declare one safe first-level /cache directory whose exact non-root UID/GID agrees across consumers."
    }
    precondition {
      condition = (
        !local.scientific_artifacts_enabled ||
        var.scientific_artifacts.storage_contract.object_storage.name != try(var.reference_data.storage_contract.object_storage.name, null)
      )
      error_message = "the scientific result store must be a bucket distinct from the reference-data plane."
    }
    precondition {
      condition = (
        !local.scientific_artifacts_enabled ||
        var.scientific_artifacts.storage_contract.region == var.target_contract.region
      )
      error_message = "the scientific artifact bucket must be in the cluster region; finalize streams every stored object back to verify its digest."
    }
  }
}
