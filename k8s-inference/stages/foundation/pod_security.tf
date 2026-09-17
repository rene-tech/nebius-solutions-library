locals {
  pod_security_receipt_required = var.pod_security_rollout_phase != "prepare"
  pod_security_baseline_artifact = local.pod_security_receipt_required ? jsondecode(
    file(var.pod_security_rollout_receipt.baseline_artifact_path)
    ) : {
    schema                                                          = "fs2-serve.nebius.ai/sai07-baseline-inventory/v5"
    inventory_sha256                                                = ""
    reference_host_paths                                            = 0
    baseline_incompatible_objects                                   = 0
    restricted_incompatible_objects                                 = 0
    collections                                                     = []
    objects                                                         = []
    legacy_controller_objects                                       = []
    legacy_service_account_token_secret_collection_resource_version = "prepare"
    legacy_service_account_token_secrets                            = []
  }
  pod_security_legacy_cleanup_names = {
    networkpolicies = sort([
      for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name
      if item.kind == "NetworkPolicy" && item.namespace == "fs2-models"
    ])
    serviceaccounts = sort([
      for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name
      if item.kind == "ServiceAccount" && item.namespace == "fs2-models"
    ])
    daemonsets = sort([
      for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name
      if item.kind == "DaemonSet" && item.namespace == "fs2-models"
    ])
  }
  pod_security_legacy_quarantine_enabled = contains([
    "cleanup-legacy-resources",
    "quiesce-enforcement",
    "enforce",
    "rollback-remove-enforcement",
    "rollback-restore-host-agents",
    "rollback-remove-exception",
  ], var.pod_security_rollout_phase)
  pod_security_legacy_networkpolicies = local.pod_security_legacy_quarantine_enabled ? {
    for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name => item
    if item.kind == "NetworkPolicy" && item.namespace == "fs2-models"
  } : {}
  pod_security_legacy_serviceaccounts = local.pod_security_legacy_quarantine_enabled ? {
    for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name => item
    if item.kind == "ServiceAccount" && item.namespace == "fs2-models"
  } : {}
  pod_security_legacy_daemonsets = local.pod_security_legacy_quarantine_enabled ? {
    for item in local.pod_security_baseline_artifact.legacy_controller_objects : item.name => item
    if item.kind == "DaemonSet" && item.namespace == "fs2-models"
  } : {}
  pod_security_scientific_namespaces = [
    "fs2-academic-poc",
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
  ]
  pod_security_retained_context = local.pod_security_receipt_required ? jsondecode(
    data.kubernetes_config_map_v1.reference_data_retained_context[0].data["context.json"]
    ) : {
    pvc = {
      namespace        = "fs2-reference-data"
      name             = "fs2-reference-data-rwx"
      uid              = "prepare"
      resource_version = "prepare"
      volume_name      = "prepare"
      storage_class    = "fs2-reference-data-retained-sc"
    }
    storage = {
      filesystem_id   = "prepare"
      capacity_gib    = 0
      claim_size_gib  = 0
      forbid_deletion = false
      retention_mode  = "prepare"
    }
    evidence = {
      read_proof_schema       = "fs2-serve.nebius.ai/reference-data-csi-readiness/v2"
      checkpoint_proof_schema = "fs2-serve.nebius.ai/checkpoint-durability-proof/v2"
      probe_image             = "prepare.invalid@sha256:${strrep("0", 64)}"
      tools_config_map        = "fs2-reference-data-tools-prepare"
      tools_data_sha256       = strrep("0", 64)
    }
  }
  pod_security_receipt_context = {
    cluster_id       = var.cluster_id
    run_id           = var.run_id
    kube_system_uid  = var.kube_system_uid
    deployment_nonce = coalesce(var.pod_security_rollout_receipt.deployment_nonce, "prepare")
    exception_admission_sha256 = sha256(jsonencode({
      host_policy_sha256     = filesha256("${path.module}/pod_security_admission.tf")
      snapshot_policy_sha256 = filesha256("${path.module}/pod_security_snapshot_admission.tf")
      rollout_custodian      = "system:serviceaccount:fs2-system:fs2-pod-security-rollout-custodian"
      external_custody_user  = coalesce(var.pod_security_rollout_receipt.custody_username, "prepare")
      external_custody_group = "fs2-pod-security-receipt-custodians"
      custody_owner_user     = coalesce(var.pod_security_rollout_receipt.custody_owner_username, "prepare")
      custody_owner_group    = var.pod_security_rollout_receipt.custody_owner_group
      platform_user          = coalesce(var.pod_security_rollout_receipt.platform_username, "prepare")
      platform_group         = var.pod_security_rollout_receipt.platform_group
      rollout_token_audience = "https://kubernetes.default.svc"
      host_agent_images      = var.pod_security_host_agent_images
      storage_probe_image    = var.pod_security_storage_probe_image
      storage_tools_config   = var.pod_security_storage_tools_config_map
      storage_generation     = var.pod_security_storage_proof_generation
      storage_attempt        = var.pod_security_storage_proof_attempt
    }))
    psa_version           = var.pod_security_version
    scientific_namespaces = local.pod_security_scientific_namespaces
    host_agents = [
      {
        component = "dcgm-exporter"
        legacy    = { namespace = "fs2-observability", name = "fs2-dcgm-exporter" }
        exception = { namespace = "fs2-node-observability", name = "fs2-dcgm-exporter" }
      },
      {
        component = "gpu-observer"
        legacy    = { namespace = "fs2-system", name = "fs2-serve-control-plane-gpu-observer" }
        exception = { namespace = "fs2-node-observability", name = "fs2-serve-control-plane-gpu-observer" }
      },
      {
        component = "node-exporter"
        legacy    = { namespace = "fs2-observability", name = "fs2-${var.run_id}-monitoring-prometheus-node-exporter" }
        exception = { namespace = "fs2-node-observability", name = "fs2-node-exporter" }
      },
      {
        component = "otel-node"
        legacy    = { namespace = "fs2-observability", name = "fs2-otel-node-agent" }
        exception = { namespace = "fs2-node-observability", name = "fs2-otel-node-agent" }
      },
    ]
    host_agent_configs = [
      {
        component   = "dcgm-cold-config"
        namespace   = "fs2-node-observability"
        name        = local.dcgm_cold_config_map_name
        data_sha256 = sha256(jsonencode({ "config.yaml" = local.dcgm_cold_config }))
      },
      {
        component   = "dcgm-metrics-config"
        namespace   = "fs2-node-observability"
        name        = local.dcgm_metrics_config_name
        data_sha256 = sha256(jsonencode({ metrics = local.dcgm_metrics }))
      },
      {
        component   = "otel-node-config"
        namespace   = "fs2-node-observability"
        name        = local.otel_node_config_map_name
        data_sha256 = sha256(jsonencode({ relay = local.otel_node_relay }))
      },
    ]
    pvc = {
      namespace        = local.pod_security_retained_context.pvc.namespace
      name             = local.pod_security_retained_context.pvc.name
      uid              = local.pod_security_retained_context.pvc.uid
      resource_version = local.pod_security_retained_context.pvc.resource_version
      volume_name      = local.pod_security_retained_context.pvc.volume_name
      storage_class    = local.pod_security_retained_context.pvc.storage_class
    }
    dataset = var.pod_security_dataset
    storage = local.pod_security_retained_context.storage
    storage_evidence = {
      read_proof_schema       = local.pod_security_retained_context.evidence.read_proof_schema
      checkpoint_proof_schema = local.pod_security_retained_context.evidence.checkpoint_proof_schema
      probe_image             = local.pod_security_retained_context.evidence.probe_image
      tools_config_map        = local.pod_security_retained_context.evidence.tools_config_map
      tools_data_sha256       = local.pod_security_retained_context.evidence.tools_data_sha256
    }
    successor_storage_sha256 = var.pod_security_successor_storage_sha256
    successor_storage        = jsondecode(var.pod_security_successor_storage_json)
    baseline = {
      schema                                          = local.pod_security_baseline_artifact.schema
      artifact_sha256                                 = local.pod_security_receipt_required ? filesha256(var.pod_security_rollout_receipt.baseline_artifact_path) : ""
      inventory_sha256                                = local.pod_security_baseline_artifact.inventory_sha256
      reference_host_paths                            = local.pod_security_baseline_artifact.reference_host_paths
      baseline_incompatible_objects                   = local.pod_security_baseline_artifact.baseline_incompatible_objects
      restricted_incompatible_objects                 = local.pod_security_baseline_artifact.restricted_incompatible_objects
      legacy_token_secret_collection_resource_version = local.pod_security_baseline_artifact.legacy_service_account_token_secret_collection_resource_version
      legacy_token_secret_count                       = length(local.pod_security_baseline_artifact.legacy_service_account_token_secrets)
    }
  }
}

data "kubernetes_config_map_v1" "reference_data_retained_context" {
  count = local.pod_security_receipt_required ? 1 : 0
  metadata {
    name      = "fs2-reference-data-retained-context"
    namespace = "fs2-system"
  }
}

data "kubernetes_persistent_volume_claim_v1" "reference_data" {
  count = local.pod_security_receipt_required ? 1 : 0
  metadata {
    name      = "fs2-reference-data-rwx"
    namespace = "fs2-reference-data"
  }
}

module "pod_security_rollout_gate" {
  source = "../../modules/pod-security-rollout-gate"

  providers = {
    kubernetes = kubernetes.pod_security_custody
  }

  consumer_role                 = "owner"
  kubeconfig_path               = var.kubeconfig_path
  kube_context                  = var.kube_context
  custody_kubeconfig_path       = var.pod_security_rollout_receipt.custody_kubeconfig_path
  custody_context               = var.pod_security_rollout_receipt.custody_context
  custody_username              = var.pod_security_rollout_receipt.custody_username
  custody_owner_kubeconfig_path = var.pod_security_rollout_receipt.custody_owner_kubeconfig_path
  custody_owner_context         = var.pod_security_rollout_receipt.custody_owner_context
  custody_owner_username        = var.pod_security_rollout_receipt.custody_owner_username
  custody_owner_group           = var.pod_security_rollout_receipt.custody_owner_group
  platform_username             = var.pod_security_rollout_receipt.platform_username
  platform_group                = var.pod_security_rollout_receipt.platform_group
  phase                         = var.pod_security_rollout_phase
  receipt_bundle_path           = var.pod_security_rollout_receipt.bundle_path
  receipt_public_key_path       = var.pod_security_rollout_receipt.public_key_path
  receipt_public_key_sha256     = var.pod_security_rollout_receipt.public_key_sha256
  baseline_artifact_path        = var.pod_security_rollout_receipt.baseline_artifact_path
  cleanup_result_path           = var.pod_security_rollout_receipt.cleanup_result_path
  receipt_key_id                = var.pod_security_rollout_receipt.key_id
  receipt_signer_identity       = var.pod_security_rollout_receipt.signer_identity
  expected_context              = local.pod_security_receipt_context

  depends_on = [
    kubernetes_manifest.pod_security_ledger_binding,
    kubernetes_manifest.pod_security_rollout_token_binding,
    kubernetes_cluster_role_binding_v1.pod_security_rollout_custodian_reader,
    kubernetes_role_binding_v1.pod_security_rollout_custodian_ledger,
  ]
}

# A phase is not complete when its receipt is consumed.  Re-read the signed
# live state and acknowledge only after every foundation-side dependency has
# applied. Exact reruns are idempotent, so a crash between CAS and state write
# resumes the same transition instead of consuming a new nonce.
module "pod_security_rollout_ack" {
  source = "../../modules/pod-security-rollout-gate"

  providers = {
    kubernetes = kubernetes.pod_security_custody
  }

  consumer_role                 = "owner"
  action                        = "acknowledge"
  kubeconfig_path               = var.kubeconfig_path
  kube_context                  = var.kube_context
  custody_kubeconfig_path       = var.pod_security_rollout_receipt.custody_kubeconfig_path
  custody_context               = var.pod_security_rollout_receipt.custody_context
  custody_username              = var.pod_security_rollout_receipt.custody_username
  custody_owner_kubeconfig_path = var.pod_security_rollout_receipt.custody_owner_kubeconfig_path
  custody_owner_context         = var.pod_security_rollout_receipt.custody_owner_context
  custody_owner_username        = var.pod_security_rollout_receipt.custody_owner_username
  custody_owner_group           = var.pod_security_rollout_receipt.custody_owner_group
  platform_username             = var.pod_security_rollout_receipt.platform_username
  platform_group                = var.pod_security_rollout_receipt.platform_group
  phase                         = var.pod_security_rollout_phase
  receipt_bundle_path           = var.pod_security_rollout_receipt.bundle_path
  receipt_public_key_path       = var.pod_security_rollout_receipt.public_key_path
  receipt_public_key_sha256     = var.pod_security_rollout_receipt.public_key_sha256
  baseline_artifact_path        = var.pod_security_rollout_receipt.baseline_artifact_path
  cleanup_result_path           = var.pod_security_rollout_receipt.cleanup_result_path
  receipt_key_id                = var.pod_security_rollout_receipt.key_id
  receipt_signer_identity       = var.pod_security_rollout_receipt.signer_identity
  expected_context              = local.pod_security_receipt_context

  depends_on = [
    kubernetes_labels.platform_pod_security,
    kubernetes_config_map_v1.otel_node_relay,
    kubernetes_manifest.node_observability_config_binding,
    kubernetes_manifest.node_observability_daemonset_binding,
    kubernetes_manifest.node_observability_pod_binding,
    kubernetes_manifest.pod_security_enforcement_fence_binding,
    kubernetes_manifest.pod_security_legacy_cleanup_fence_binding,
    helm_release.node_exporter_exception,
    helm_release.otel_node_exception,
  ]
}

resource "terraform_data" "pod_security_rollout_contract" {
  input = module.pod_security_rollout_gate.verification

  lifecycle {
    precondition {
      condition = !local.pod_security_receipt_required || (
        local.pod_security_retained_context.pvc.uid == data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].uid &&
        local.pod_security_retained_context.pvc.resource_version == data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].resource_version &&
        local.pod_security_retained_context.pvc.volume_name == data.kubernetes_persistent_volume_claim_v1.reference_data[0].spec[0].volume_name &&
        local.pod_security_retained_context.pvc.storage_class == data.kubernetes_persistent_volume_claim_v1.reference_data[0].spec[0].storage_class_name &&
        local.pod_security_retained_context.storage.retention_mode == "retain" &&
        local.pod_security_retained_context.storage.forbid_deletion
      )
      error_message = "The signed rollout context must match the live retained PVC UID/class and deletion-protected storage contract."
    }
  }
}

resource "kubernetes_labels" "platform_pod_security" {
  for_each = var.pod_security_rollout_phase == "enforce" ? toset([
    "fs2-data",
    "fs2-models",
    "fs2-observability",
    "fs2-system",
  ]) : []

  api_version = "v1"
  kind        = "Namespace"
  metadata {
    name = each.value
  }
  labels = {
    "pod-security.kubernetes.io/enforce"         = "baseline"
    "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
    "pod-security.kubernetes.io/audit"           = "restricted"
    "pod-security.kubernetes.io/audit-version"   = var.pod_security_version
    "pod-security.kubernetes.io/warn"            = "restricted"
    "pod-security.kubernetes.io/warn-version"    = var.pod_security_version
  }
  force = false

  depends_on = [terraform_data.pod_security_rollout_contract]
}
