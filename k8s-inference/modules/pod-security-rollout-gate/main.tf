/*
REJECTED IN-PROCESS IMPLEMENTATION (retained as exact negative evidence).

This block previously created the monotonic ledger and invoked its mutating
consumer from the platform Terraform process while that process also received
platform, receipt-custodian, and custody-owner kubeconfigs.  It is deliberately
inactive: a Terraform invocation cannot be both the protected platform actor
and the owner of the boundary that constrains it.

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

# The separately authenticated custody provider owns the only rollout ledger.
# Its data is initialized once, then changed exclusively by the verifier's
# resourceVersion-guarded replace. Platform Terraform must have no authority to
# reset or destroy the monotonic history.
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
      FS2_KUBECONFIG                  = coalesce(var.custody_kubeconfig_path, "/prepare-not-authorized")
      FS2_KUBE_CONTEXT                = coalesce(var.custody_context, "prepare")
      FS2_PLATFORM_KUBECONFIG         = var.kubeconfig_path
      FS2_PLATFORM_KUBE_CONTEXT       = var.kube_context
      FS2_POD_SECURITY_CUSTODY_USER   = coalesce(var.custody_username, "prepare")
      FS2_CUSTODY_OWNER_KUBECONFIG    = coalesce(var.custody_owner_kubeconfig_path, "/prepare-not-authorized")
      FS2_CUSTODY_OWNER_KUBE_CONTEXT  = coalesce(var.custody_owner_context, "prepare")
      FS2_CUSTODY_OWNER_USER          = coalesce(var.custody_owner_username, "prepare")
      FS2_CUSTODY_OWNER_GROUP         = var.custody_owner_group
      FS2_PLATFORM_USER               = coalesce(var.platform_username, "prepare")
      FS2_PLATFORM_GROUP              = var.platform_group
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
        && var.custody_kubeconfig_path != null
        && var.custody_context != null
        && var.custody_username != null
        && var.custody_owner_kubeconfig_path != null
        && var.custody_owner_context != null
        && var.custody_owner_username != null
        && var.platform_username != null
        && try(abspath(var.custody_kubeconfig_path) != abspath(var.kubeconfig_path), false)
        && try(abspath(var.custody_owner_kubeconfig_path) != abspath(var.kubeconfig_path), false)
        && try(abspath(var.custody_owner_kubeconfig_path) != abspath(var.custody_kubeconfig_path), false)
        && var.custody_owner_username != var.custody_username
        && var.custody_owner_username != var.platform_username
        && var.custody_username != var.platform_username
        && var.custody_owner_group != var.platform_group
        && var.custody_owner_group != "fs2-pod-security-receipt-custodians"
        && var.platform_group != "fs2-pod-security-receipt-custodians"
        && (var.phase != "quiesce-enforcement" || var.cleanup_result_path != null)
      )
      error_message = "Every post-prepare phase requires a whole-bundle signature plus three distinct identities and kubeconfigs: platform Terraform, external receipt operator, and separately administered custody owner."
    }
  }

  depends_on = [kubernetes_config_map_v1.ledger]
}
*/

locals {
  # Even `prepare` requires an external execution acknowledgement. No platform
  # state address is transferred: the separately pinned executor owns only one
  # additive immutable acknowledgement object and proves every retained object
  # unchanged across that SSA. A prepare acknowledgement binds the zero digest.
  receipt_required       = var.phase != "prepare"
  receipt_bundle_sha256  = local.receipt_required ? filesha256(var.receipt_bundle_path) : null
  expected_bundle_sha256 = local.receipt_required ? local.receipt_bundle_sha256 : strrep("0", 64)
  external_acknowledgement_query = {
    ack_path                      = var.external_handoff_path
    trust_lock_path               = "${path.module}/../../stages/pod-security-custody/custody-trust-lock-v3.json"
    receipt_bundle_sha256         = local.expected_bundle_sha256
    expected_context_sha256       = sha256(jsonencode(var.expected_context))
    expected_custody_epoch_sha256 = var.custody_epoch_sha256
    expected_phase                = var.phase
    expected_consumer             = var.consumer_role
    expected_action               = var.action
    cluster_id                    = var.expected_context.cluster_id
    kube_system_uid               = var.expected_context.kube_system_uid
    platform_kubeconfig_path      = var.kubeconfig_path
    platform_context              = var.kube_context
  }
}

# This clock updates in place on every plan/apply attempt. It is retained and
# never replaced or destroyed. Because the acknowledgement data source depends
# on a pending clock update, Terraform defers the read until apply even when
# phase and context are unchanged. A delayed saved plan therefore evaluates the
# acknowledgement, exact saved-plan bytes and ten-minute expiry at apply time.
# Planning does not read an acknowledgement: the external executor receives
# that saved plan, signs its full projection, and writes the generation file
# before apply. The apply process must export FS2_SAI07_APPLY_PLAN_PATH pointing
# at the exact saved plan it is executing.
resource "terraform_data" "apply_freshness_clock" {
  input = {
    attempted_at         = timestamp()
    acknowledgement_path = var.external_handoff_path
    action               = var.action
    consumer             = var.consumer_role
    phase                = var.phase
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The platform invocation has no custody provider and receives no receipt,
# owner, or token-minting kubeconfig.  It can only validate a short-lived,
# whole-file signed v3 acknowledgement emitted by the independently
# administered executor after phase-ledger consumption and immediate before/
# after full-object reads. The signing key and provider/backend trust facts come
# only from the repository v3 lock; legacy key variables are ignored and cannot
# select authority. At apply it performs only authenticated Kubernetes
# SelfSubjectReview/SSRR/SSAR reads for the actual platform transport; it has no
# mutation path.
data "external" "verified_execution_acknowledgement" {
  program = ["python3", "${path.module}/../../scripts/verify_sai07_external_execution_ack_v3.py"]

  query = local.external_acknowledgement_query

  depends_on = [terraform_data.apply_freshness_clock]
}

resource "terraform_data" "verified" {
  input = data.external.verified_execution_acknowledgement.result

  lifecycle {
    # A new signed generation updates this state-only gate in place. Replacement
    # would violate the no-delete contract and is unnecessary because the
    # apply-deferred external data source performs the full verification.
    prevent_destroy = true
    precondition {
      condition = (
        var.external_handoff_path != null &&
        var.custody_epoch_sha256 != strrep("0", 64) &&
        data.external.verified_execution_acknowledgement.result.valid == "true" &&
        data.external.verified_execution_acknowledgement.result.bundle_sha256 == local.expected_bundle_sha256 &&
        data.external.verified_execution_acknowledgement.result.custody_epoch_sha256 == var.custody_epoch_sha256 &&
        data.external.verified_execution_acknowledgement.result.phase == var.phase &&
        data.external.verified_execution_acknowledgement.result.consumer == var.consumer_role &&
        data.external.verified_execution_acknowledgement.result.action == var.action
      )
      error_message = "A fresh independently signed v3 external execution acknowledgement matching this exact phase, consumer, action, bundle and context is required."
    }
  }
}
