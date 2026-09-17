terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # Partial configuration is supplied from the root-owned release-automation
  # backend file.  Local/default-local state is forbidden even for the
  # normalized deployment contract because it contains resource identities
  # used by the credential custody fence.
  backend "s3" {}

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "= 2.3.5"
    }
  }
}
