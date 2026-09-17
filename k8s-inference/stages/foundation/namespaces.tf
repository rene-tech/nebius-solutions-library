resource "kubernetes_namespace_v1" "platform" {
  for_each = local.namespaces

  metadata {
    name = each.value
    labels = merge(
      local.common_labels,
      { "kubernetes.io/metadata.name" = each.value },
      each.value == "fs2-models" ? {
        "pod-security.kubernetes.io/audit"         = "restricted"
        "pod-security.kubernetes.io/audit-version" = "latest"
        "pod-security.kubernetes.io/warn"          = "restricted"
        "pod-security.kubernetes.io/warn-version"  = "latest"
      } : {},
    )
  }

  depends_on = [terraform_data.cluster_contract]
}
