terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  backend "local" {}

  required_providers {
    # SAI-20 verifies the independently signed, Git-bound database-network
    # authority handoff before Terraform can construct any protected object.
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
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
