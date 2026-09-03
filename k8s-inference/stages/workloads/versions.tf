terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  backend "local" {}

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "= 3.2.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 3.2.1"
    }
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.232"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.7.2"
    }
  }
}
