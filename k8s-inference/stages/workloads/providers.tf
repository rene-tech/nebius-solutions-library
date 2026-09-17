provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}

provider "nebius" {
  # The profile remains a signed authority selector in the handoff. Provider
  # authentication comes only from the accepted capsule's sealed, short-lived
  # token descriptor and never from ambient HOME/profile configuration.
  token = chomp(file(var.nebius_iam_token_file))
}
