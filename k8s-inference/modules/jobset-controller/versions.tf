terraform {
  required_version = ">= 1.9.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.1"
    }
    # The chart archive is materialized during plan, so the Helm provider can
    # resolve the exact local bytes it will install instead of pulling its own
    # copy of the same reference.
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
