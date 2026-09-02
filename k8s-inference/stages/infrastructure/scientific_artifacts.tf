# Scientific result storage.
#
# This is a distinct bucket and a distinct identity from the model cache and
# the reference-data plane, so the two are never destroyed or rotated together.
# It is nonetheless disposable with the run by default, like everything else
# this stage owns: a destroy removes it.
#
# Two opt-in ways to keep results beyond the run, neither of them the default:
# forbid_deletion = true marks the bucket prevent_destroy, which makes a
# destroy fail until the flag is cleared; create_bucket = false binds a bucket
# this stage does not own, which keeps results outside the run's lifecycle
# without blocking teardown at all.

locals {
  scientific_artifacts_enabled = var.scientific_artifacts.enabled
  scientific_artifacts_created = local.scientific_artifacts_enabled && var.scientific_artifacts.create_bucket
  scientific_artifacts_bound   = local.scientific_artifacts_enabled && !var.scientific_artifacts.create_bucket

  # The object-storage provider exposes no deletion-protection field, and
  # Terraform's prevent_destroy takes a literal rather than an expression, so
  # the two retention semantics are two mutually exclusive resources. Exactly
  # one of them ever exists, and flipping the flag on a retained bucket is
  # refused rather than silently converted.
  scientific_artifacts_retained   = local.scientific_artifacts_created && var.scientific_artifacts.forbid_deletion
  scientific_artifacts_disposable = local.scientific_artifacts_created && !var.scientific_artifacts.forbid_deletion

  scientific_artifacts_bucket_id = coalesce(
    one(nebius_storage_v1_bucket.scientific_artifacts_retained[*].id),
    one(nebius_storage_v1_bucket.scientific_artifacts_disposable[*].id),
    one(data.nebius_storage_v1_bucket.scientific_artifacts[*].id),
    "absent",
  )

  scientific_artifacts_bucket_name = local.scientific_artifacts_enabled ? var.scientific_artifacts.bucket_name : null
  scientific_artifacts_endpoint    = local.scientific_artifacts_enabled ? "https://storage.${var.scientific_artifacts.region}.nebius.cloud" : null
}

resource "terraform_data" "scientific_artifacts_contract" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  input = {
    bucket_name          = var.scientific_artifacts.bucket_name
    create_bucket        = local.scientific_artifacts_created
    bind_existing_bucket = local.scientific_artifacts_bound
    bucket_lifecycle = (
      local.scientific_artifacts_retained ? "retained" :
      local.scientific_artifacts_disposable ? "disposable" : "bound"
    )
    forbid_deletion      = var.scientific_artifacts.forbid_deletion
    versioning_policy    = var.scientific_artifacts.versioning_policy
    max_size_bytes       = var.scientific_artifacts.max_size_bytes
    secret_delivery_mode = var.scientific_artifacts.secret_delivery_mode
    region               = var.scientific_artifacts.region
    endpoint             = local.scientific_artifacts_endpoint
  }

  lifecycle {
    precondition {
      condition     = var.scientific_artifacts.region == local.selected_target.region
      error_message = "Scientific artifact storage must be regional with the cluster; finalize streams every stored object back to verify its digest."
    }
  }
}

# Opt-in only, selected by forbid_deletion = true. Terraform refuses to destroy
# this bucket, which means a stage teardown fails until the operator clears the
# flag and applies, or removes the bucket from state. That is a deliberate
# trade the operator chooses; it is not the default and is not required.
resource "nebius_storage_v1_bucket" "scientific_artifacts_retained" {
  count = local.scientific_artifacts_retained ? 1 : 0

  parent_id         = var.project_id
  name              = var.scientific_artifacts.bucket_name
  versioning_policy = var.scientific_artifacts.versioning_policy
  max_size_bytes    = var.scientific_artifacts.max_size_bytes
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "retained"
  })

  lifecycle {
    prevent_destroy = true

    # Renaming would orphan every committed content address, so a name change
    # must be a reviewed migration rather than a silent replacement.
    ignore_changes = [name]
  }

  depends_on = [
    terraform_data.target_contract,
    terraform_data.scientific_artifacts_contract,
  ]
}

# The default. The bucket is owned by the run and is destroyed with it, so a
# teardown of this stage is complete and leaves nothing behind.
resource "nebius_storage_v1_bucket" "scientific_artifacts_disposable" {
  count = local.scientific_artifacts_disposable ? 1 : 0

  parent_id         = var.project_id
  name              = var.scientific_artifacts.bucket_name
  versioning_policy = var.scientific_artifacts.versioning_policy
  max_size_bytes    = var.scientific_artifacts.max_size_bytes
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "ephemeral"
  })

  lifecycle {
    ignore_changes = [name]
  }

  depends_on = [
    terraform_data.target_contract,
    terraform_data.scientific_artifacts_contract,
  ]
}

data "nebius_storage_v1_bucket" "scientific_artifacts" {
  count = local.scientific_artifacts_bound ? 1 : 0

  parent_id = var.project_id
  name      = var.scientific_artifacts.bucket_name
}

resource "nebius_iam_v1_service_account" "scientific_artifacts_writer" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-artifacts"
  description = "Scientific artifact writer for fs2 Terraform lifecycle ${var.run_id}"
  labels      = merge(local.common_labels, { purpose = "scientific-artifact-write" })

  depends_on = [
    terraform_data.target_contract,
    terraform_data.scientific_artifacts_contract,
  ]
}

resource "nebius_iam_v1_group" "scientific_artifacts_writers" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = data.nebius_iam_v2_project.target.id
  name      = "${local.resource_name}-artifact-writers"
  labels    = merge(local.common_labels, { purpose = "scientific-artifact-write" })

  depends_on = [
    terraform_data.target_contract,
    terraform_data.scientific_artifacts_contract,
  ]
}

resource "nebius_iam_v1_group_membership" "scientific_artifacts_writer" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
  member_id = nebius_iam_v1_service_account.scientific_artifacts_writer[0].id
}

# The permit is scoped to this one bucket, not to the project, so the key
# cannot read the model cache, the registry, or any other tenant's data.
resource "nebius_iam_v1_access_permit" "scientific_artifacts_writer" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = nebius_iam_v1_group.scientific_artifacts_writers[0].id
  resource_id = local.scientific_artifacts_bucket_id
  role        = "editor"
}

resource "nebius_iam_v2_access_key" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-artifacts"
  description = "Scientific artifact S3 key for fs2 Terraform lifecycle ${var.run_id}"
  labels      = merge(local.common_labels, { purpose = "scientific-artifact-write" })

  account = {
    service_account = {
      id = nebius_iam_v1_service_account.scientific_artifacts_writer[0].id
    }
  }

  # INLINE returns the secret to Terraform so the workloads stage can project it
  # into one Kubernetes Secret in the same lifecycle. It is therefore held in
  # this stage's state and nowhere else: workloads receives it as an ephemeral
  # value and writes it write-only, so it never reaches workloads state or any
  # Helm release value. Choose MYSTERY_BOX to keep it out of Terraform entirely
  # and deliver the Secret out of band.
  secret_delivery_mode = var.scientific_artifacts.secret_delivery_mode

  # Both the membership and the permit must land first, so the key is never
  # briefly valid for an identity that is not yet authorized on the bucket.
  depends_on = [
    nebius_iam_v1_group_membership.scientific_artifacts_writer,
    nebius_iam_v1_access_permit.scientific_artifacts_writer,
  ]
}
