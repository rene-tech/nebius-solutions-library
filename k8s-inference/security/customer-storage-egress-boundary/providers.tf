provider "kubernetes" {
  config_path    = pathexpand(var.security_owner_kubeconfig_path)
  config_context = var.security_owner_kube_context
}
