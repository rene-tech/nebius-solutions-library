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

data "external" "credential_migration_gate" {
  program = ["python3", "${path.module}/../../scripts/secret_migration_guard.py", "native-gate", "--registry", "${path.module}/../../security/durable-credential-registry.json"]
  query = {
    receipt_path            = var.credential_migration_gate_receipt_path
    terraform_configuration = path.module
    terraform_root          = "foundation"
    source_commit           = var.credential_migration_gate_source_commit
  }
}

resource "terraform_data" "credential_migration_gate" {
  input = data.external.credential_migration_gate.result.receipt_sha256
  # Every plan must execute the saved-plan/state validation at apply time.
  triggers_replace = [data.external.credential_migration_gate.result.receipt_sha256, timestamp()]

  provisioner "local-exec" {
    command = "python3 ${path.module}/../../scripts/secret_migration_guard.py apply-saved-plan-gate --terraform-configuration ${path.module} --terraform-root foundation --source-commit ${var.credential_migration_gate_source_commit} --registry ${path.module}/../../security/durable-credential-registry.json"
  }

  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = data.external.credential_migration_gate.result.status == "pass"
      error_message = "The short-lived credential migration gate did not pass. Use inference-stack; direct apply without an exact gate receipt is forbidden."
    }
  }
}
