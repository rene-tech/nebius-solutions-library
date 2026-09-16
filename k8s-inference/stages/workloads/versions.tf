terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # This root contains durable credential history and therefore has no local
  # backend mode.  Release automation supplies a root-owned partial S3 config.
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
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.232"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.7.2"
    }
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
  }
}
