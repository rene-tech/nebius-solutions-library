variable "owner_kubeconfig_path" {
  description = "Absolute kubeconfig for the separately administered custody owner; never a platform Terraform credential."
  type        = string
  sensitive   = true
  validation {
    condition     = startswith(var.owner_kubeconfig_path, "/") && !strcontains(var.owner_kubeconfig_path, "..")
    error_message = "owner_kubeconfig_path must be absolute without parent traversal."
  }
}

variable "owner_context" {
  type = string
}

variable "manifest_bundle_path" {
  type      = string
  sensitive = true
}

variable "secret_metadata_artifact_path" {
  description = "Deprecated and ignored for pre-SSA admission. The anchor uses atomic typed POST creation; metadata-only evidence is collected after creation."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "iam_boundary_receipt_path" {
  type      = string
  sensitive = true
}

variable "backend_custody_receipt_path" {
  type      = string
  sensitive = true
}
