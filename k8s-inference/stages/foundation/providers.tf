provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Versioned identity-epoch rotation uses a dedicated, <=15-minute bootstrap
# identity preauthorized by the prior epoch. It cannot create or delete the
# externally installed boundary. It is distinct from the ordinary release and
# name-scoped runtime identities; workloads remain gated until it expires.
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
