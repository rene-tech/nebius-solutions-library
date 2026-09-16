resource "kubernetes_namespace_v1" "platform" {
  for_each = local.namespaces

  metadata {
    name = each.value
    labels = merge(
      local.common_labels,
      { "kubernetes.io/metadata.name" = each.value },
      contains([
        "fs2-system",
        local.control_plane_network_policy_gateway_namespace,
        local.control_plane_network_policy_controller_namespace,
      ], each.value) ? { "fs2.nebius.ai/network-policy-boundary" = "permanent" } : {},
    )
  }

  depends_on = [terraform_data.cluster_contract]
}
