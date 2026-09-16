terraform {
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.1"
    }
  }
}

provider "helm" {
  kubernetes = {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}

module "mindeval_workshop" {
  source         = "../../modules/mindeval-workshop"
  namespace      = var.namespace
  workshop_image = var.workshop_image
  public_origin  = var.public_origin
  values         = var.values
}

output "workshop_url" {
  value = module.mindeval_workshop.workshop_url
}

output "workshop_api_url" {
  value = module.mindeval_workshop.workshop_api_url
}

output "gateway_api_url" {
  value = module.mindeval_workshop.gateway_api_url
}
