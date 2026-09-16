provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Admission ownership is deliberately separate from the ordinary foundation
# and workload identity. The preflight in control_plane_network_policy_boundary.tf
# proves the ordinary identity cannot remove this policy or impersonate its owner.
provider "kubernetes" {
  alias          = "network_policy_security_owner"
  config_path    = pathexpand(local.control_plane_network_policy_security_owner_kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
