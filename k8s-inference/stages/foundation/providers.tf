provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# SAI-07 custody is a separate trust domain. The platform provider must never
# own the rollout identity, its RBAC, its monotonic ledger, or the admission
# policies that protect those objects.
provider "kubernetes" {
  alias = "pod_security_custody"

  config_path = pathexpand(coalesce(
    var.pod_security_rollout_receipt.custody_owner_kubeconfig_path,
    "/pod-security-custody-owner-not-configured",
  ))
  config_context = coalesce(
    var.pod_security_rollout_receipt.custody_owner_context,
    "pod-security-custody-owner-not-configured",
  )
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
