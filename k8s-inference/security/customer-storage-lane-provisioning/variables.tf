variable "security_owner_nebius_profile" {
  description = "Dedicated stable-lane provisioning profile, distinct from workloads and admission owners."
  type        = string
  validation {
    condition     = var.security_owner_nebius_profile != "" && !contains(["sandbox", "default"], var.security_owner_nebius_profile)
    error_message = "A named dedicated lane-provisioning profile is required."
  }
}

variable "provisioning_manifest_json" {
  description = "Externally approved signed manifest containing provider inputs only; it cannot contain Node or admission attestations."
  type        = string
  sensitive   = true
  validation {
    condition     = can(jsondecode(var.provisioning_manifest_json)) && length(var.provisioning_manifest_json) <= 1048576
    error_message = "provisioning_manifest_json must be bounded JSON."
  }
}
