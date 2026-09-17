output "fence_generations" {
  description = "Value-free append-only webhook identities for independent live equality verification."
  value = {
    for generation, fence in kubernetes_manifest.daemonset_fence : generation => {
      name = fence.object.metadata.name
      uid  = fence.object.metadata.uid
      spec = fence.object.webhooks
      cutover_executor_username = var.fence_generations[generation].cutover_executor_username
      cutover_executor_uid = var.fence_generations[generation].cutover_executor_uid
      cutover_executor_credential_id = var.fence_generations[generation].cutover_executor_credential_id
      cutover_executor_valid_until = var.fence_generations[generation].cutover_executor_valid_until
      receipt_authority_registry_sha256 = var.fence_generations[generation].receipt_authority_registry_sha256
      activation_trust_uid      = kubernetes_config_map_v1.activation_trust[generation].metadata[0].uid
      activation_trust_immutable = kubernetes_config_map_v1.activation_trust[generation].immutable
      cutover_deployment_names  = sort(tolist(var.fence_generations[generation].cutover_deployment_names))
      cutover_role_uid          = kubernetes_role_v1.cutover_executor[generation].metadata[0].uid
      cutover_role_rules        = kubernetes_role_v1.cutover_executor[generation].rule
      cutover_binding_uid       = kubernetes_role_binding_v1.cutover_executor[generation].metadata[0].uid
      cutover_binding_subjects  = kubernetes_role_binding_v1.cutover_executor[generation].subject
    }
  }
}
