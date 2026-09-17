output "fence_generations" {
  description = "Value-free append-only webhook identities for independent live equality verification."
  value = {
    for generation, fence in kubernetes_manifest.daemonset_fence : generation => {
      name = fence.object.metadata.name
      uid  = fence.object.metadata.uid
      spec = fence.object.webhooks
      cutover_executor_username = var.fence_generations[generation].cutover_executor_username
      cutover_deployment_names  = sort(tolist(var.fence_generations[generation].cutover_deployment_names))
      cutover_role_uid          = kubernetes_role_v1.cutover_executor[generation].metadata[0].uid
      cutover_role_rules        = kubernetes_role_v1.cutover_executor[generation].rule
      cutover_binding_uid       = kubernetes_role_binding_v1.cutover_executor[generation].metadata[0].uid
      cutover_binding_subjects  = kubernetes_role_binding_v1.cutover_executor[generation].subject
    }
  }
}
