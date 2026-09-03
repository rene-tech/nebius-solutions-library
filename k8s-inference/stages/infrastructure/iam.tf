resource "nebius_iam_v1_service_account" "nodepull" {
  parent_id   = var.project_id
  name        = "${local.resource_name}-nodepull"
  description = "Ephemeral node identity for fs2 Terraform lifecycle ${var.run_id}"
  labels      = merge(local.common_labels, { purpose = "node-registry-pull" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group" "target_registry_readers" {
  # Access permits inherit the group's scope, so the run-owned registry uses a
  # group in the target project. External registries receive their own groups
  # in the projects that own them below; no tenant-level IAM write is needed.
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
  # Managed Kubernetes node credential exchange currently requires the
  # node-group service account to hold viewer at project scope. A viewer permit
  # attached only to the Registry resource is accepted by IAM but kubelet image
  # pulls receive 403 for manifests that are not already cached on the node.
  # Keep this in the target project (rather than a tenant default group), and
  # retain the dedicated run-owned group so destroy removes the grant.
  parent_id   = nebius_iam_v1_group.target_registry_readers.id
  resource_id = data.nebius_iam_v2_project.target.id
  role        = "viewer"
}

resource "nebius_iam_v1_group" "external_registry_readers" {
  for_each = data.nebius_registry_v1_registry.external

  parent_id = each.value.parent_id
  name      = "${local.resource_name}-external-${substr(sha256(each.key), 0, 8)}-readers"
  labels    = merge(local.common_labels, { purpose = "external-registry-read" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_group_membership" "nodepull_external_registry" {
  for_each = data.nebius_registry_v1_registry.external

  parent_id = nebius_iam_v1_group.external_registry_readers[each.key].id
  member_id = nebius_iam_v1_service_account.nodepull.id
}

resource "nebius_iam_v1_access_permit" "nodepull_external_registry" {
  for_each = data.nebius_registry_v1_registry.external

  parent_id   = nebius_iam_v1_group.external_registry_readers[each.key].id
  resource_id = each.key
  role        = "viewer"
}
