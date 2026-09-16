terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
