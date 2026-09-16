variable "phase" {
  type = string
  validation {
    condition = contains([
      "prepare",
      "migrate-reference-data",
      "cleanup-legacy-resources",
      "enforce",
      "rollback-remove-enforcement",
      "rollback-restore-host-agents",
      "rollback-remove-exception",
    ], var.phase)
    error_message = "phase must name one ordered SAI-07 rollout or rollback state."
  }
}

variable "receipt_bundle_path" {
  type      = string
  default   = null
  nullable  = true
  sensitive = true
}

variable "receipt_public_key_path" {
  type      = string
  default   = null
  nullable  = true
  sensitive = true
}

variable "receipt_public_key_sha256" {
  type      = string
  default   = null
  nullable  = true
  sensitive = true
  validation {
    condition     = var.receipt_public_key_sha256 == null || can(regex("^[a-f0-9]{64}$", var.receipt_public_key_sha256))
    error_message = "receipt_public_key_sha256 must be a lowercase SHA-256 digest."
  }
}

variable "expected_context" {
  type      = any
  sensitive = true
}
