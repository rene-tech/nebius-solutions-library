# Dedicated scientific result artifact store.
#
# This is deliberately a second, separate object store. The reference-data
# bucket holds immutable public science inputs that are expensive to rebuild;
# this bucket holds tenant result bytes with a different retention, a different
# writer identity and a different blast radius. Neither bucket's key, policy or
# lifecycle is widened to serve the other.
#
# The writer identity is scoped to the canonical `scientific/v1/` root through
# the bucket policy, so even a leaked key cannot read or write anything else in
# the project. The S3 secret itself is delivered through Nebius MysteryBox and
# is therefore never present in this stage's state, plan or outputs; only the
# access-key ID, the opaque secret reference and a revision travel downstream.

locals {
  scientific_artifacts_enabled = var.scientific_artifacts.enabled
  scientific_artifacts_retain  = local.scientific_artifacts_enabled && var.scientific_artifacts.lifecycle.retention_mode == "retain"
  scientific_artifacts_dispose = local.scientific_artifacts_enabled && var.scientific_artifacts.lifecycle.retention_mode == "disposable"

  # The canonical object root. Every committed artifact is addressed by tenant,
  # operation, stage, shard, attempt, direction and content digest, so one
  # tenant's prefix can never overlap another's and a rerun never overwrites a
  # committed object.
  scientific_artifacts_root        = "scientific/v1"
  scientific_artifacts_path_scope  = "scientific/v1/*"
  scientific_artifacts_writer_role = "storage.object-editor"

  # Storage-side hygiene only. Expiring a *current* object is an application
  # decision made against the durable result record, so no rule here deletes
  # live artifacts; these three rules only reclaim aborted uploads, superseded
  # versions and the tombstones left behind by application deletes.
  scientific_artifacts_lifecycle_rules = [
    {
      id                                = "abort-incomplete-multipart-uploads"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = { days_after_initiation = 1 }
      expiration                        = null
      noncurrent_version_expiration     = null
      noncurrent_version_transition     = null
      transition                        = null
    },
    {
      id                                = "expire-noncurrent-versions"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = null
      expiration                        = null
      noncurrent_version_expiration     = { noncurrent_days = 1, newer_noncurrent_versions = null }
      noncurrent_version_transition     = null
      transition                        = null
    },
    {
      id                                = "remove-expired-delete-markers"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = null
      expiration                        = { expired_object_delete_marker = true, days = null, date = null }
      noncurrent_version_expiration     = null
      noncurrent_version_transition     = null
      transition                        = null
    },
  ]

  scientific_artifacts_bucket_id = local.scientific_artifacts_enabled ? (
    local.scientific_artifacts_retain ?
    one(nebius_storage_v1_bucket.scientific_artifacts[*].id) :
    one(nebius_storage_v1_bucket.scientific_artifacts_disposable[*].id)
  ) : null
  scientific_artifacts_bucket_name = local.scientific_artifacts_enabled ? (
    local.scientific_artifacts_retain ?
    one(nebius_storage_v1_bucket.scientific_artifacts[*].name) :
    one(nebius_storage_v1_bucket.scientific_artifacts_disposable[*].name)
  ) : null
  scientific_artifacts_endpoint = "https://storage.${local.selected_target.region}.nebius.cloud"
}

resource "terraform_data" "scientific_artifacts_contract" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  input = {
    bucket_name     = var.scientific_artifacts.object_storage.bucket_name
    max_size_gib    = var.scientific_artifacts.object_storage.max_size_gib
    retention_mode  = var.scientific_artifacts.lifecycle.retention_mode
    retention_days  = var.scientific_artifacts.retention_days
    region          = local.selected_target.region
    object_root     = local.scientific_artifacts_root
    writer_role     = local.scientific_artifacts_writer_role
    writer_paths    = [local.scientific_artifacts_path_scope]
    lifecycle_rules = [for rule in local.scientific_artifacts_lifecycle_rules : rule.id]
    secret_delivery = "MYSTERY_BOX"
  }

  lifecycle {
    precondition {
      condition     = var.scientific_artifacts.object_storage.bucket_name != var.reference_data.object_storage.bucket_name
      error_message = "the scientific result store must be a distinct bucket from the reference-data plane; results and immutable public inputs never share retention, policy or a writer key."
    }
    precondition {
      condition = (
        local.scientific_artifacts_retain ==
        (var.scientific_artifacts.lifecycle.retention_mode == "retain")
      )
      error_message = "scientific artifact retention mode must resolve to exactly one of the retained or disposable bucket resources."
    }
  }

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_service_account" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-scientific-artifacts"
  description = "Least-privilege writer for the dedicated scientific result artifact store"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-writer"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })

  depends_on = [terraform_data.scientific_artifacts_contract]
}

resource "nebius_iam_v1_group" "scientific_artifacts_writers" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = var.project_id
  name      = "${local.resource_name}-scientific-artifact-writers"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-write"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })

  depends_on = [terraform_data.scientific_artifacts_contract]
}

resource "nebius_iam_v1_group_membership" "scientific_artifacts_writer" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
  member_id = nebius_iam_v1_service_account.scientific_artifacts[0].id
}

resource "nebius_storage_v1_bucket" "scientific_artifacts" {
  count = local.scientific_artifacts_retain ? 1 : 0

  parent_id             = var.project_id
  name                  = var.scientific_artifacts.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.scientific_artifacts.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "durable"
  })
  bucket_policy = {
    rules = [{
      group_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
      paths    = [local.scientific_artifacts_path_scope]
      roles    = [local.scientific_artifacts_writer_role]
    }]
  }
  lifecycle_configuration = {
    rules = local.scientific_artifacts_lifecycle_rules
  }

  depends_on = [nebius_iam_v1_group_membership.scientific_artifacts_writer]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_storage_v1_bucket" "scientific_artifacts_disposable" {
  count = local.scientific_artifacts_dispose ? 1 : 0

  parent_id             = var.project_id
  name                  = var.scientific_artifacts.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.scientific_artifacts.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "disposable-empty-only"
  })
  bucket_policy = {
    rules = [{
      group_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
      paths    = [local.scientific_artifacts_path_scope]
      roles    = [local.scientific_artifacts_writer_role]
    }]
  }
  lifecycle_configuration = {
    rules = local.scientific_artifacts_lifecycle_rules
  }

  depends_on = [nebius_iam_v1_group_membership.scientific_artifacts_writer]
}

resource "nebius_iam_v2_access_key" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-scientific-artifacts"
  description = "S3 access key for the dedicated scientific result artifact store"
  # MysteryBox is the only supported delivery for this key. An INLINE key would
  # place the S3 secret in this stage's state and in every plan file produced
  # from it, which the artifact-store contract forbids.
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-write"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.scientific_artifacts[0].id
    }
  }

  # The bucket policy is the only thing that authorizes this key, so it must
  # exist before the key does; otherwise the key is briefly valid for an
  # identity with no scope at all.
  depends_on = [
    nebius_storage_v1_bucket.scientific_artifacts,
    nebius_storage_v1_bucket.scientific_artifacts_disposable,
  ]
}
