variable "phase" {
  type = string
  validation {
    condition = contains([
      "prepare",
      "bootstrap-baseline",
      "migrate-reference-data",
      "cleanup-legacy-resources",
      "quiesce-enforcement",
      "enforce",
      "rollback-remove-enforcement",
      "rollback-restore-host-agents",
      "rollback-remove-exception",
    ], var.phase)
    error_message = "phase must name one ordered SAI-07 rollout or rollback state."
  }
}

variable "action" {
  description = "Authorize before dependent resources, then acknowledge the same exact authorization after apply."
  type        = string
  default     = "authorize"
  validation {
    condition     = contains(["authorize", "acknowledge"], var.action)
    error_message = "action must be authorize or acknowledge."
  }
}

variable "consumer_role" {
  description = "Foundation owns and advances the ledger; workloads consumes its exact authorization once."
  type        = string
  validation {
    condition     = contains(["owner", "downstream"], var.consumer_role)
    error_message = "consumer_role must be owner or downstream."
  }
}

variable "kubeconfig_path" {
  type      = string
  sensitive = true
  validation {
    condition     = startswith(var.kubeconfig_path, "/") && !strcontains(var.kubeconfig_path, "..")
    error_message = "kubeconfig_path must be absolute without parent traversal."
  }
}

variable "kube_context" {
  type = string
  validation {
    condition     = length(var.kube_context) >= 1 && length(var.kube_context) <= 253
    error_message = "kube_context must be a bounded exact context name."
  }
}

variable "custody_kubeconfig_path" {
  description = "Absolute kubeconfig for the separately administered external OIDC custody principal; never the platform Terraform kubeconfig."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true

  validation {
    condition = var.custody_kubeconfig_path == null || (
      startswith(var.custody_kubeconfig_path, "/") && !strcontains(var.custody_kubeconfig_path, "..")
    )
    error_message = "custody_kubeconfig_path must be absolute without parent traversal."
  }
}

variable "custody_context" {
  description = "Exact context selected from the external-custody kubeconfig."
  type        = string
  default     = null
  nullable    = true
}

variable "custody_username" {
  description = "Exact non-system OIDC username independently assigned to receipt custody."
  type        = string
  default     = null
  nullable    = true
}

variable "custody_owner_kubeconfig_path" {
  description = "Absolute kubeconfig for the separately administered Terraform owner of custody objects."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "custody_owner_context" {
  description = "Exact context selected from the custody-owner kubeconfig."
  type        = string
  default     = null
  nullable    = true
}

variable "custody_owner_username" {
  description = "Exact non-system identity that owns custody RBAC, admission, and ledger resources."
  type        = string
  default     = null
  nullable    = true
}

variable "custody_owner_group" {
  description = "Dedicated group required on the custody-owner identity."
  type        = string
  default     = "fs2-pod-security-custody-owners"
}

variable "platform_username" {
  description = "Exact platform Terraform identity excluded from custody ownership."
  type        = string
  default     = null
  nullable    = true
}

variable "platform_group" {
  description = "Dedicated platform Terraform group excluded from custody ownership."
  type        = string
  default     = "fs2-platform-terraform"
}

variable "token_audience" {
  description = "Exact Kubernetes API audience for the ten-minute rollout-custodian TokenRequest."
  type        = string
  default     = "https://kubernetes.default.svc"
  validation {
    condition     = var.token_audience == "https://kubernetes.default.svc"
    error_message = "token_audience must equal the reviewed Kubernetes API audience."
  }
}

variable "ledger_namespace" {
  type    = string
  default = "fs2-system"
}

variable "ledger_name" {
  type    = string
  default = "fs2-pod-security-rollout-ledger"
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

variable "baseline_artifact_path" {
  description = "Descriptor-fenced frozen pre-migration inventory artifact read again by the apply-time verifier."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "cleanup_result_path" {
  description = "Exact executed cleanup result consumed only by the cleanup-complete/quiesce transition."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
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

variable "receipt_key_id" {
  type     = string
  default  = null
  nullable = true
}

variable "receipt_signer_identity" {
  type     = string
  default  = null
  nullable = true
}

variable "expected_context" {
  type      = any
  sensitive = true
}
