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

variable "owner_username" {
  type = string
}
variable "owner_group" {
  type    = string
  default = "fs2-pod-security-custody-owners"
}
variable "platform_username" {
  type = string
}
variable "platform_group" {
  type    = string
  default = "fs2-platform-terraform"
}
variable "cluster_id" {
  type = string
}

variable "kube_system_uid" {
  type = string
}

variable "manifest_bundle_path" {
  type      = string
  sensitive = true
}

variable "manifest_public_key_path" {
  type      = string
  sensitive = true
}

variable "manifest_public_key_sha256" {
  type = string
}

variable "manifest_key_id" {
  type = string
}
variable "iam_boundary_sha256" {
  description = "Digest of the independently issued IAM/group-exclusion receipt; this root cannot issue its own boundary."
  type        = string
}
