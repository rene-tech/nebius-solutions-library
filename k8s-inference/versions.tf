terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  # This state contains only the normalized, non-secret deployment contract.
  # The orchestrator supplies a deployment-scoped local backend path.
  backend "local" {}
}
