resource "nebius_iam_v1_service_account" "nodepull" {
  parent_id   = var.project_id
  name        = "${local.resource_name}-nodepull"
  description = "Ephemeral node identity for fs2 Terraform lifecycle ${var.run_id}"
  labels      = merge(local.common_labels, { purpose = "node-registry-pull" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group" "target_registry_readers" {
  parent_id = data.nebius_iam_v2_project.target.id
  name      = "${local.resource_name}-target-readers"
  labels    = merge(local.common_labels, { purpose = "target-registry-read" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group_membership" "nodepull_target_registry" {
  parent_id = nebius_iam_v1_group.target_registry_readers.id
  member_id = nebius_iam_v1_service_account.nodepull.id
}

resource "nebius_iam_v1_access_permit" "nodepull_registry" {
  parent_id   = nebius_iam_v1_group.target_registry_readers.id
  resource_id = nebius_registry_v1_registry.images.id
  role        = "viewer"
}

resource "nebius_iam_v1_access_permit" "nodepull_external_registry" {
  for_each = var.external_registry_ids

  parent_id   = nebius_iam_v1_group.target_registry_readers.id
  resource_id = each.value
  role        = "viewer"
}
