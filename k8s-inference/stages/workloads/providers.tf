provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Network-boundary objects are never authored as, or impersonated by, the
# shared deployment credential. Platform Security supplies a separate
# kubeconfig whose server-side username is checked by inference-stack before
# planning. Prepare and armed transitions use different credentials.
provider "kubernetes" {
  alias          = "network_boundary"
  config_path    = pathexpand(var.model_network_boundary_kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}

provider "helm" {
  alias = "control_plane"
  kubernetes = {
    config_path = pathexpand(
      var.model_network_helm_kubeconfig_path == "" ?
      var.kubeconfig_path : var.model_network_helm_kubeconfig_path
    )
    config_context = var.kube_context
  }
}

provider "nebius" {
  profile = {
    name            = var.nebius_profile
    no_browser_open = true
  }
}
