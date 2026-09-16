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
  profile = {
    name            = var.nebius_profile
    no_browser_open = true
  }
}

# Ephemeral password generation is local-only. A distinct configuration lets
# terraform test mock the default provider used by persistent random_id values.
provider "random" {
  alias = "ephemeral"
}
