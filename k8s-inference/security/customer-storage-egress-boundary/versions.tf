terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # Canonical security-owner and release generations share a versioned,
  # locked remote backend configured only by the external security owner.
  backend "s3" {}

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
