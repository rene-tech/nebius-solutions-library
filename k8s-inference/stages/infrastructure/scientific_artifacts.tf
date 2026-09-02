# Durable scientific result storage.
#
# This is intentionally a distinct bucket and a distinct identity from the
# model cache and the reference-data plane. That cache is rebuildable from
# upstream and is disposable with the run; these are tenant results held under
# a retention contract, so the bucket defaults to versioned and undeletable and
# survives a cluster teardown. Set create_bucket = false to bind an existing
# shared artifact plane instead of provisioning a second one.

locals {
  scientific_artifacts_enabled = var.scientific_artifacts.enabled
  scientific_artifacts_created = local.scientific_artifacts_enabled && var.scientific_artifacts.create_bucket
  scientific_artifacts_bound   = local.scientific_artifacts_enabled && !var.scientific_artifacts.create_bucket

  scientific_artifacts_bucket_id = local.scientific_artifacts_created ? one(
    nebius_storage_v1_bucket.scientific_artifacts[*].id
    ) : (
    local.scientific_artifacts_bound ? one(data.nebius_storage_v1_bucket.scientific_artifacts[*].id) : null
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

    precondition {
      condition     = !var.scientific_artifacts.create_bucket || var.scientific_artifacts.versioning_policy == "ENABLED"
      error_message = "A Terraform-created scientific artifact bucket must be versioned; committed results are immutable evidence."
    }
  }
}

resource "nebius_storage_v1_bucket" "scientific_artifacts" {
  count = local.scientific_artifacts_created ? 1 : 0

  parent_id         = var.project_id
  name              = var.scientific_artifacts.bucket_name
  versioning_policy = var.scientific_artifacts.versioning_policy
  max_size_bytes    = var.scientific_artifacts.max_size_bytes
  labels            = merge(local.common_labels, { purpose = "scientific-artifact-results" })

  lifecycle {
    # Results outlive the disposable cluster that produced them. Renaming the
    # bucket would orphan every committed content address, so a name change
    # must be a reviewed migration rather than a silent replacement.
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
