# Scientific artifact store credential projection.
#
# The generated object-storage key arrives as an ephemeral variable and is
# written with the provider's write-only argument, so it never lands in this
# stage's state, in a plan file, or in a Helm release value. Rotating the key
# changes the revision below, which is what triggers a rewrite.

locals {
  scientific_artifacts_enabled = var.scientific_artifacts.enabled
  scientific_artifacts_secret  = "fs2-serve-artifact-store"

  scientific_artifacts_credential_revision = (
    local.scientific_artifacts_enabled && var.scientific_artifacts.access_key_id != null
    ? parseint(substr(sha256(var.scientific_artifacts.access_key_id), 0, 8), 16)
    : 0
  )

  # Built through a zero-or-one comprehension so the disabled case yields an
  # empty map rather than an object Terraform cannot unify with the enabled one.
  scientific_artifacts_overrides = merge([
    for _ in range(local.scientific_artifacts_enabled ? 1 : 0) : {
      scientificArtifacts = {
        enabled          = true
        endpoint         = var.scientific_artifacts.endpoint
        bucket           = var.scientific_artifacts.bucket_name
        region           = var.scientific_artifacts.region
        addressingStyle  = var.scientific_artifacts.addressing_style
        verifyTls        = var.scientific_artifacts.verify_tls
        handleTtlSeconds = var.scientific_artifacts.handle_ttl_seconds
        maxBytes         = var.scientific_artifacts.max_bytes
        retentionSeconds = var.scientific_artifacts.retention_seconds
        mediaTypes       = sort(var.scientific_artifacts.media_types)
        egressCidrs      = sort(var.scientific_artifacts.egress_cidrs)
      }
      secrets = {
        artifactStore = {
          name = local.scientific_artifacts_secret
          key  = "credentials.json"
        }
      }
    }
  ]...)
}

resource "terraform_data" "scientific_artifacts_contract" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  input = {
    bucket_name        = var.scientific_artifacts.bucket_name
    region             = var.scientific_artifacts.region
    endpoint           = var.scientific_artifacts.endpoint
    secret_name        = local.scientific_artifacts_secret
    handle_ttl_seconds = var.scientific_artifacts.handle_ttl_seconds
    retention_seconds  = var.scientific_artifacts.retention_seconds
    egress_configured  = length(var.scientific_artifacts.egress_cidrs) > 0
    # Exactly what the control-plane chart is told. It is entirely non-secret:
    # the credential is ephemeral and deliberately absent from this receipt,
    # and the precondition below proves it was supplied.
    chart_values = local.scientific_artifacts_overrides
    # Derived from the key identity alone, so rotating the key rewrites the
    # Secret without ever recording the secret itself.
    credential_revision = local.scientific_artifacts_credential_revision
  }

  lifecycle {
    precondition {
      condition     = var.scientific_artifact_store_credentials != null
      error_message = "Enabling the scientific artifact store requires the generated object-storage credential from the infrastructure stage."
    }

    precondition {
      condition     = var.scientific_artifacts.region == local.selected_target.region
      error_message = "The scientific artifact bucket must be in the cluster region; digest verification streams every stored object back."
    }

    precondition {
      condition     = length(var.scientific_artifacts.egress_cidrs) > 0
      error_message = "The scientific artifact store needs at least one object-storage egress CIDR; presigning is local but digest verification is not."
    }
  }
}

resource "kubernetes_secret_v1" "scientific_artifact_store" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = local.scientific_artifacts_secret
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-purpose" = "scientific-artifact-store" })
  }

  type = "Opaque"

  data_wo = {
    "credentials.json" = jsonencode({
      access_key_id     = var.scientific_artifact_store_credentials.access_key_id
      secret_access_key = var.scientific_artifact_store_credentials.secret_access_key
    })
  }
  data_wo_revision = local.scientific_artifacts_credential_revision

  depends_on = [
    terraform_data.cluster_contract,
    terraform_data.scientific_artifacts_contract,
  ]
}
