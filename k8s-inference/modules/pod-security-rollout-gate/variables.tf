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
  description = "Retired fail-closed input. Platform Terraform must never receive the external receipt kubeconfig."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true

  validation {
    condition     = var.custody_kubeconfig_path == null
    error_message = "custody_kubeconfig_path must be null; custody transitions run outside platform Terraform."
  }
}

variable "custody_context" {
  description = "Retired fail-closed input; must remain null."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.custody_context == null
    error_message = "custody_context must be null in platform Terraform."
  }
}

variable "custody_username" {
  description = "Retired fail-closed input; identity is bound by the external handoff."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.custody_username == null
    error_message = "custody_username must be null in platform Terraform."
  }
}

variable "custody_owner_kubeconfig_path" {
  description = "Retired fail-closed input. The external custody root is a separate invocation."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
  validation {
    condition     = var.custody_owner_kubeconfig_path == null
    error_message = "custody_owner_kubeconfig_path must be null in platform Terraform."
  }
}

variable "custody_owner_context" {
  description = "Retired fail-closed input; must remain null."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.custody_owner_context == null
    error_message = "custody_owner_context must be null in platform Terraform."
  }
}

variable "custody_owner_username" {
  description = "Expected owner identity bound by the external handoff; no owner credential is accepted."
  type        = string
  default     = null
  nullable    = true
}

variable "external_handoff_path" {
  description = "Absolute path to the short-lived whole-file signed handoff emitted by the independent custody root."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "external_handoff_public_key_path" {
  description = "Deprecated and ignored. Handoff authority is pinned only by the repository custody trust lock."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "external_handoff_public_key_sha256" {
  description = "Deprecated and ignored. Handoff authority is pinned only by the repository custody trust lock."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition = (
      var.external_handoff_public_key_sha256 == null ||
      can(regex("^[a-f0-9]{64}$", var.external_handoff_public_key_sha256))
    )
    error_message = "external_handoff_public_key_sha256 must be a lowercase SHA-256."
  }
}

variable "external_handoff_key_id" {
  description = "Deprecated and ignored. Handoff authority is pinned only by the repository custody trust lock."
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
