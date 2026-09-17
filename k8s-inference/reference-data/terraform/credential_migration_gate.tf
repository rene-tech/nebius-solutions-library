variable "credential_migration_gate_managed_by_parent" {
  description = "Must be true: reference-data is supported only as a workloads-owned child module."
  type        = bool
  default     = false
  validation {
    condition     = var.credential_migration_gate_managed_by_parent
    error_message = "Standalone reference-data Terraform is unsupported; instantiate this module only from the workloads root."
  }
}

variable "credential_migration_gate_parent_token" {
  description = "Opaque ID of the workloads-owned native gate. Only the child marker and protected credential resources consume it."
  type        = string
  default     = ""
}

variable "credential_migration_gate_receipt_path" {
  description = "Owner-only, short-lived SAI-10 gate receipt. Direct plans/applies fail closed when it is absent."
  type        = string
  default     = ""
}

variable "credential_migration_gate_source_commit" {
  description = "Exact lowercase source commit bound into the SAI-10 gate receipt."
  type        = string
  default     = ""
}

variable "credential_migration_gate_receipt_sha256" {
  description = "Exact SHA-256 of the unique short-lived receipt represented by this additive gate generation."
  type        = string
  default     = ""
  validation {
    condition     = var.credential_migration_gate_receipt_sha256 == "" || can(regex("^[0-9a-f]{64}$", var.credential_migration_gate_receipt_sha256))
    error_message = "credential_migration_gate_receipt_sha256 must be lowercase SHA-256."
  }
}

variable "credential_migration_gate_history" {
  description = "All previously applied gate receipt hashes; omission is rejected by prevent_destroy."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for digest in var.credential_migration_gate_history : can(regex("^[0-9a-f]{64}$", digest))])
    error_message = "Every credential migration gate history entry must be lowercase SHA-256."
  }
}

variable "credential_migration_phase" {
  description = "Explicit two-phase custody mode for append-only credential Secrets."
  type        = string
  default     = "steady"
  validation {
    condition     = contains(["steady", "secret-stage", "consumer-rollout"], var.credential_migration_phase)
    error_message = "credential_migration_phase must be steady, secret-stage, or consumer-rollout."
  }
}

data "external" "credential_migration_gate" {
  program = ["/usr/bin/python3", "/opt/fs2/k8s-inference/scripts/secret_migration_guard.py", "forwarded-native-gate"]
  query = {
    receipt_path            = var.credential_migration_gate_receipt_path
    terraform_configuration = path.root
    terraform_root          = "workloads"
    source_commit           = var.credential_migration_gate_source_commit
  }
}

resource "terraform_data" "credential_migration_gate" {
  input = {
    parent_gate_token = var.credential_migration_gate_parent_token
    receipt_sha256    = data.external.credential_migration_gate.result.receipt_sha256
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [input]
    precondition {
      condition = (
        var.credential_migration_gate_managed_by_parent &&
        var.credential_migration_gate_parent_token != "" &&
        path.root != path.module &&
        data.external.credential_migration_gate.result.status == "pass" &&
        data.external.credential_migration_gate.result.receipt_sha256 == var.credential_migration_gate_receipt_sha256 &&
        !contains(var.credential_migration_gate_history, var.credential_migration_gate_receipt_sha256)
      )
      error_message = "Reference-data requires the exact workloads-owned native receipt and cannot run as a standalone or arbitrarily embedded root."
    }
  }
}
