terraform {
  required_version = ">= 1.10.0, < 2.0.0"

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
  }
}
