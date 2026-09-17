provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Admission/bootstrap writes use a separately custodied, short-lived release
# identity. Falling back here only lets Terraform initialize configuration;
# the fixed-boundary and Helm preconditions refuse that fallback before any
# protected resource can be planned or adopted.
provider "kubernetes" {
  alias          = "release_identity"
  config_path    = pathexpand(var.release_identity_kubeconfig_path != "" ? var.release_identity_kubeconfig_path : var.kubeconfig_path)
  config_context = var.release_identity_kube_context != "" ? var.release_identity_kube_context : var.kube_context
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
