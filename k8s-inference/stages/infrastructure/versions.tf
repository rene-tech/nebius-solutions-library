terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # Root-owned release automation supplies the isolated object/key/workspace
  # configuration.  State must remain encrypted and access logged remotely.
  backend "s3" {}

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.232"
    }
  }
}

provider "nebius" {
  profile = {
    name            = var.nebius_profile
    no_browser_open = true
  }
}
