terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  backend "local" {}

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "= 3.2.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 3.2.1"
    }
  }
}
