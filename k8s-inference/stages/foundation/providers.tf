provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

# Retention-only alias for the existing custody addresses already recorded in
# the platform state. It deliberately uses the same platform credential: no
# owner credential enters this Terraform invocation, and no second state may
# adopt or forget these objects. The external v3 executor owns zero fields on
# them. Exact external IAM/Kubernetes audits must prove this identity has read
# access for refresh but cannot mutate the protected objects before activation.
provider "kubernetes" {
  alias          = "pod_security_custody"
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
