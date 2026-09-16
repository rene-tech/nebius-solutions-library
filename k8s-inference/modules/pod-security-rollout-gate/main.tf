locals {
  receipt_required = var.phase != "prepare"
  terminal_states = {
    "migrate-reference-data"       = "exception-ready"
    "cleanup-legacy-resources"     = "reference-data-ready"
    "enforce"                      = "baseline-ready"
    "rollback-remove-enforcement"  = "baseline-enforced"
    "rollback-restore-host-agents" = "enforcement-removed"
    "rollback-remove-exception"    = "host-agents-restored"
  }
  terminal_sequences = {
    "migrate-reference-data"       = 1
    "cleanup-legacy-resources"     = 2
    "enforce"                      = 3
    "rollback-remove-enforcement"  = 4
    "rollback-restore-host-agents" = 5
    "rollback-remove-exception"    = 6
  }
  bundle_sha256 = local.receipt_required ? filesha256(var.receipt_bundle_path) : null
  initial_ledger = {
    schema         = "fs2-serve.nebius.ai/pod-security-rollout-ledger/v1"
    context_sha256 = sha256(jsonencode(var.expected_context))
    authority = {
      key_id            = coalesce(var.receipt_key_id, "prepare")
      signer_identity   = coalesce(var.receipt_signer_identity, "prepare")
      public_key_sha256 = coalesce(var.receipt_public_key_sha256, "prepare")
    }
    sequence           = 0
    state              = "unmanaged"
    last_bundle_sha256 = null
    last_receipt_id    = null
    last_nonce         = null
    authorization      = null
  }
  consume_query = local.receipt_required ? {
    mode                     = var.consumer_role == "owner" ? "owner-transition" : "downstream-authorization"
    receipt_path             = var.receipt_bundle_path
    public_key_path          = var.receipt_public_key_path
    public_key_sha256        = var.receipt_public_key_sha256
    baseline_artifact_path   = var.baseline_artifact_path
    cleanup_result_path      = var.cleanup_result_path
    expected_key_id          = var.receipt_key_id
    expected_signer_identity = var.receipt_signer_identity
    expected_context         = var.expected_context
    expected_phase           = var.phase
    ledger_namespace         = var.ledger_namespace
    ledger_name              = var.ledger_name
  } : null
}

# The foundation state owns the only rollout ledger. Its data is initialized
# once, then changed exclusively by the verifier's resourceVersion-guarded
# replace. Terraform must neither reset nor destroy the monotonic history.
resource "kubernetes_config_map_v1" "ledger" {
  count = var.consumer_role == "owner" && local.receipt_required ? 1 : 0

  metadata {
    name      = var.ledger_name
    namespace = var.ledger_namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "fs2-serve"
      "security.fs2.nebius.ai/role"  = "pod-security-rollout-ledger"
    }
  }

  data = {
    schema                            = local.initial_ledger.schema
    context_sha256                    = local.initial_ledger.context_sha256
    authority_key_id                  = local.initial_ledger.authority.key_id
    authority_signer_identity         = local.initial_ledger.authority.signer_identity
    authority_public_key_sha256       = local.initial_ledger.authority.public_key_sha256
    sequence                          = tostring(local.initial_ledger.sequence)
    state                             = local.initial_ledger.state
    last_bundle_sha256                = ""
    last_receipt_id                   = ""
    last_nonce                        = ""
    authorization_phase               = ""
    authorization_bundle_sha256       = ""
    authorization_nonce               = ""
    authorization_downstream_consumed = ""
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [data]
  }
}

# This is deliberately apply-time and state-changing. A Terraform data source
# would run during plan and could only re-check a replayable file. The owner
# verifies live objects and advances the ledger; the workloads stage then
# consumes the exact downstream authorization once. Both updates use the live
# ConfigMap resourceVersion as an optimistic-concurrency precondition.
resource "terraform_data" "verified" {
  count = local.receipt_required ? 1 : 0

  input = {
    phase          = var.phase
    terminal_state = local.terminal_states[var.phase]
    bundle_sha256  = local.bundle_sha256
    sequence       = local.terminal_sequences[var.phase]
    consumer       = var.consumer_role
  }

  triggers_replace = {
    phase           = var.phase
    consumer        = var.consumer_role
    bundle_sha256   = local.bundle_sha256
    context_sha256  = sha256(jsonencode(var.expected_context))
    verifier_sha256 = filesha256("${path.module}/../../scripts/verify_pod_security_receipts.py")
  }

  provisioner "local-exec" {
    command = "python3 \"${path.module}/../../scripts/verify_pod_security_receipts.py\""
    quiet   = true

    environment = {
      FS2_KUBECONFIG         = var.kubeconfig_path
      FS2_KUBE_CONTEXT       = var.kube_context
      FS2_POD_SECURITY_QUERY = jsonencode(local.consume_query)
    }
  }

  lifecycle {
    precondition {
      condition = (
        var.receipt_bundle_path != null &&
        var.receipt_public_key_path != null &&
        var.receipt_public_key_sha256 != null &&
        var.receipt_key_id != null &&
        var.receipt_signer_identity != null
        && var.baseline_artifact_path != null
        && (var.phase != "enforce" || var.cleanup_result_path != null)
      )
      error_message = "Every post-prepare phase requires a whole-bundle signature, reviewed signer identity, descriptor-fenced baseline artifact, and durable-ledger consumption."
    }
  }

  depends_on = [kubernetes_config_map_v1.ledger]
}
