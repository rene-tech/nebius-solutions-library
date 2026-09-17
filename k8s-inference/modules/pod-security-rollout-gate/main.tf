locals {
  receipt_required = var.phase != "prepare"
  terminal_states = {
    "bootstrap-baseline"           = "baseline-captured"
    "migrate-reference-data"       = "exception-ready"
    "cleanup-legacy-resources"     = "reference-data-ready"
    "quiesce-enforcement"          = "enforcement-quiesced"
    "enforce"                      = "baseline-enforced"
    "rollback-remove-enforcement"  = "enforcement-removed"
    "rollback-restore-host-agents" = "host-agents-restored"
    "rollback-remove-exception"    = "rolled-back"
  }
  terminal_sequences = {
    "bootstrap-baseline"           = 1
    "migrate-reference-data"       = 2
    "cleanup-legacy-resources"     = 3
    "quiesce-enforcement"          = 4
    "enforce"                      = 5
    "rollback-remove-enforcement"  = 6
    "rollback-restore-host-agents" = 7
    "rollback-remove-exception"    = 8
  }
  bundle_sha256           = local.receipt_required ? filesha256(var.receipt_bundle_path) : null
  proof_generation_ledger = local.receipt_required ? var.expected_context.successor_storage.proof_generation_ledger : null
  proof_generation_ids = local.receipt_required ? [
    for sequence in range(1, length(local.proof_generation_ledger.generations) + 1) : one([
      for generation_id, generation in local.proof_generation_ledger.generations : generation_id
      if generation.sequence == sequence
    ])
  ] : []
  stable_expected_context = local.receipt_required ? merge(var.expected_context, {
    exception_admission_sha256 = "proof-generation-render-bound-by-signed-bundle"
    successor_storage_sha256   = "proof-generations-bound-by-ledger-v3"
    successor_storage = merge(var.expected_context.successor_storage, {
      proof_generation_ledger = {
        schema              = local.proof_generation_ledger.schema
        maximum_generations = local.proof_generation_ledger.maximum_generations
      }
    })
  }) : var.expected_context
  initial_ledger = {
    schema         = "fs2-serve.nebius.ai/pod-security-rollout-ledger/v3"
    context_sha256 = sha256(jsonencode(local.stable_expected_context))
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
    proof_generations = local.receipt_required ? {
      ids           = local.proof_generation_ids
      ledger_sha256 = sha256(jsonencode(local.proof_generation_ledger))
      sequence      = length(local.proof_generation_ids)
      active        = local.proof_generation_ledger.active_generation
    } : null
    authorization = null
  }
  consume_query = local.receipt_required ? {
    mode = var.action == "acknowledge" ? (
      var.consumer_role == "owner" ? "owner-acknowledgement" : "downstream-acknowledgement"
      ) : (
      var.consumer_role == "owner" ? "owner-transition" : "downstream-authorization"
    )
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
  count = var.consumer_role == "owner" && var.action == "authorize" && local.receipt_required ? 1 : 0

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
    schema                                = local.initial_ledger.schema
    context_sha256                        = local.initial_ledger.context_sha256
    authority_key_id                      = local.initial_ledger.authority.key_id
    authority_signer_identity             = local.initial_ledger.authority.signer_identity
    authority_public_key_sha256           = local.initial_ledger.authority.public_key_sha256
    sequence                              = tostring(local.initial_ledger.sequence)
    state                                 = local.initial_ledger.state
    last_bundle_sha256                    = ""
    last_receipt_id                       = ""
    last_nonce                            = ""
    proof_generation_ids                  = jsonencode(local.initial_ledger.proof_generations.ids)
    proof_generation_ledger_sha256        = local.initial_ledger.proof_generations.ledger_sha256
    proof_generation_sequence             = tostring(local.initial_ledger.proof_generations.sequence)
    proof_generation_active               = local.initial_ledger.proof_generations.active
    authorization_phase                   = ""
    authorization_bundle_sha256           = ""
    authorization_nonce                   = ""
    authorization_owner_acknowledged      = ""
    authorization_downstream_acknowledged = ""
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
    action         = var.action
  }

  triggers_replace = {
    phase           = var.phase
    consumer        = var.consumer_role
    action          = var.action
    bundle_sha256   = local.bundle_sha256
    context_sha256  = sha256(jsonencode(local.stable_expected_context))
    verifier_sha256 = filesha256("${path.module}/../../scripts/verify_pod_security_receipts.py")
  }

  provisioner "local-exec" {
    command = "python3 \"${path.module}/../../scripts/verify_pod_security_receipts.py\""
    quiet   = true

    environment = {
      FS2_KUBECONFIG                  = var.kubeconfig_path
      FS2_KUBE_CONTEXT                = var.kube_context
      FS2_POD_SECURITY_TOKEN_AUDIENCE = var.token_audience
      FS2_POD_SECURITY_QUERY          = jsonencode(local.consume_query)
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
        && (var.phase != "quiesce-enforcement" || var.cleanup_result_path != null)
      )
      error_message = "Every post-prepare phase requires a whole-bundle signature, reviewed signer identity, descriptor-fenced baseline artifact, and durable-ledger consumption."
    }
  }

  depends_on = [kubernetes_config_map_v1.ledger]
}
