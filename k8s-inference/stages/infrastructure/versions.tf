terraform {
  required_version = ">= 1.11.0, < 2.0.0"

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
  # The accepted capsule writes no credential into configuration or state.
  # It passes a sealed, signed, short-lived token descriptor whose exact
  # authority/profile binding is independently verified before Terraform
  # starts. file() reads only that inherited /proc/self/fd snapshot.
  token = chomp(file(var.nebius_iam_token_file))
}
