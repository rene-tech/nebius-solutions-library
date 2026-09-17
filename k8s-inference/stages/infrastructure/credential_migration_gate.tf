variable "credential_migration_gate_receipt_path" {
  description = "Owner-only, short-lived credential migration gate receipt. Direct plans/applies fail closed when it is absent."
  type        = string
  default     = ""
}

variable "credential_migration_gate_source_commit" {
  description = "Exact lowercase source commit bound into the credential migration gate receipt."
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
  description = "Explicit two-phase custody mode for append-only provider credentials."
  type        = string
  default     = "steady"
  validation {
    condition     = contains(["steady", "secret-stage", "consumer-rollout"], var.credential_migration_phase)
    error_message = "credential_migration_phase must be steady, secret-stage, or consumer-rollout."
  }
}

data "external" "credential_migration_gate" {
  program = ["/usr/bin/python3", "/opt/fs2/k8s-inference/scripts/secret_migration_guard.py", "native-gate"]
  query = {
    receipt_path            = var.credential_migration_gate_receipt_path
    terraform_configuration = path.module
    terraform_root          = "infrastructure"
    source_commit           = var.credential_migration_gate_source_commit
  }
}

resource "terraform_data" "credential_migration_gate" {
  input      = data.external.credential_migration_gate.result.receipt_sha256
  depends_on = [terraform_data.credential_apply_gate_generation]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [input]
    precondition {
      condition = (
        data.external.credential_migration_gate.result.status == "pass" &&
        data.external.credential_migration_gate.result.receipt_sha256 == var.credential_migration_gate_receipt_sha256
      )
      error_message = "The short-lived credential migration gate did not pass. Use inference-stack; direct apply without an exact gate receipt is forbidden."
    }
  }
}

resource "terraform_data" "credential_apply_gate_generation" {
  for_each = setunion(var.credential_migration_gate_history, toset([var.credential_migration_gate_receipt_sha256]))
  input    = each.key

  provisioner "local-exec" {
    command = "/usr/bin/python3 /opt/fs2/k8s-inference/scripts/secret_migration_guard.py apply-saved-plan-gate --terraform-configuration ${path.module} --terraform-root infrastructure --source-commit ${var.credential_migration_gate_source_commit}"
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.external.credential_migration_gate.result.status == "pass" &&
        data.external.credential_migration_gate.result.receipt_sha256 == var.credential_migration_gate_receipt_sha256
      )
      error_message = "The short-lived credential migration gate did not pass. Use inference-stack; direct apply without an exact gate receipt is forbidden."
    }
  }
}

# Non-secret, authoritative feature activation.  Credential custody reads this
# exact state address instead of inferring enablement from a missing key.  The
# marker is permanent; toggling a feature updates only its boolean value.
resource "terraform_data" "credential_feature_activation" {
  input = {
    schema = "fs2-serve.nebius.ai/credential-feature-activation/v1"
    activations = {
      "reference-data-s3" = {
        "reference-data" = var.reference_data.enabled
      }
      "scientific-artifact-s3" = {
        "scientific-artifacts" = var.scientific_artifacts.enabled
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
