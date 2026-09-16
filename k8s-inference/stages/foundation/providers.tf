provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Creation and versioned identity-epoch rotation use a dedicated, <=15-minute
# bootstrap identity. It is distinct from both the ordinary release identity
# and the name-scoped runtime enforcer; workloads remain gated until it expires.
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
