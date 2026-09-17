provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# SAI-07 custody is deliberately absent from this provider graph. The separate
# stages/pod-security-custody root owns admission, custody RBAC, the token
# anchor and the monotonic ledger under a separately administered identity.
# Platform Terraform receives only its signed, short-lived handoff artifact.

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
