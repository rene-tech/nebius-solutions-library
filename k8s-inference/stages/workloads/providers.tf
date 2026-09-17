provider "kubernetes" {
  # This path is an immutable memfd retained by inference-stack for the whole
  # plan/apply pair. The durable run-owned pathname is never reopened here.
  config_path    = var.provider_kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = var.provider_kubeconfig_path
    config_context = var.kube_context
  }
}

provider "nebius" {
  profile = {
    name            = var.nebius_profile
    no_browser_open = true
  }
}
