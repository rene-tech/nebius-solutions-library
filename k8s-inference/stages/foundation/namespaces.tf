resource "kubernetes_namespace_v1" "platform" {
  for_each = local.namespaces

  metadata {
    name   = each.value
    labels = merge(local.common_labels, { "kubernetes.io/metadata.name" = each.value })
  }

  depends_on = [terraform_data.cluster_contract]
}
