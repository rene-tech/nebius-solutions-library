provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Creation uses a dedicated, short-lived bootstrap identity. It is distinct
# from both the ordinary release identity and the name-scoped runtime enforcer.
provider "kubernetes" {
  alias          = "network_policy_security_owner"
  config_path    = pathexpand(local.control_plane_network_policy_security_bootstrap_kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
