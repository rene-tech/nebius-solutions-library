provider "kubernetes" {
  config_path    = pathexpand(var.security_owner_kubeconfig_path)
  config_context = var.security_owner_kube_context
}

provider "helm" {
  alias = "storage_release"
  kubernetes = {
    config_path = pathexpand(
      var.non_owner_identities[var.release_identity_name].kubeconfig_path
    )
    config_context = var.non_owner_identities[var.release_identity_name].kube_context
  }
}
