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
  scientific_artifacts_enabled     = var.scientific_artifacts.enabled
  scientific_artifacts_secret_name = "fs2-serve-artifact-store"
  scientific_artifacts_secret_key  = "credentials.json"
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
      schedulingContractConfigMapName = kubernetes_config_map_v1.scientific_scheduling_contract.metadata[0].name
      schedulingContractKey           = local.scheduling_contract_key
      schedulingContractSha256        = local.scheduling_contract_sha256
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
