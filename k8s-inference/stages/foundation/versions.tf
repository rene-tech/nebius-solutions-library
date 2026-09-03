terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  backend "local" {}

  required_providers {
    # The chart archive is materialized during plan so the Helm provider can
    # resolve the exact local bytes it installs.
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
  }
}
