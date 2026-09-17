provider "kubernetes" {
  config_path    = var.provider_kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = var.provider_kubeconfig_path
    config_context = var.kube_context
  }
}
