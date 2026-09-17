terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # Avoid Terraform's implicit local backend for artifact handoff state.
  backend "s3" {}
}
