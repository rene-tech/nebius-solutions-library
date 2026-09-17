terraform {
  required_version = ">= 1.10.0"

  # This is deliberately a partial backend configuration.  Bucket, region,
  # endpoints and credentials are supplied only by the separately administered
  # custody pipeline and are authenticated by the signed backend receipt.  A
  # local/default state is never a valid custody boundary.
  backend "s3" {
    key          = "sai07/pod-security-custody/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.36"
    }
  }
}
