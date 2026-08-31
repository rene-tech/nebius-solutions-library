terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  # Every lifecycle supplies a run-scoped path with -backend-config. The
  # retained fs2-serve backend is never opened by this root.
  backend "local" {}

  required_providers {
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
