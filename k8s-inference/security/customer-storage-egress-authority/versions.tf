terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # Canonical applies require the security-owner's versioned, locked remote
  # backend configuration. Local/alternate state is not an authority source.
  backend "s3" {}

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = "= 0.5.232"
    }
  }
}
