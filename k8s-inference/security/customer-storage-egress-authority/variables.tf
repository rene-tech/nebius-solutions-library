variable "security_owner_nebius_profile" {
  description = "Dedicated provider-security owner profile; never a workloads or release profile."
  type        = string

  validation {
    condition     = var.security_owner_nebius_profile != "" && !contains(["sandbox", "default"], var.security_owner_nebius_profile)
    error_message = "A named dedicated provider-security owner profile is required."
  }
}

variable "authority_manifest_json" {
  description = "Externally approved, signed, append-only provider-egress authority ledger."
  type        = string
  sensitive   = true

  validation {
    condition     = can(jsondecode(var.authority_manifest_json)) && length(var.authority_manifest_json) <= 1048576
    error_message = "authority_manifest_json must be bounded JSON."
  }
}
