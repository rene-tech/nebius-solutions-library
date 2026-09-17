locals {
  existing_scientific_pod_security_labels = {
    "pod-security.kubernetes.io/enforce"         = "baseline"
    "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
    "pod-security.kubernetes.io/audit"           = "restricted"
    "pod-security.kubernetes.io/audit-version"   = var.pod_security_version
    "pod-security.kubernetes.io/warn"            = "restricted"
    "pod-security.kubernetes.io/warn-version"    = var.pod_security_version
  }
  pod_security_receipt_required = var.pod_security_rollout_phase != "prepare"
  pod_security_baseline_artifact = local.pod_security_receipt_required ? jsondecode(
    file(var.pod_security_rollout_receipt.baseline_artifact_path)
    ) : {
    schema                          = "fs2-serve.nebius.ai/sai07-baseline-inventory/v4"
    inventory_sha256                = ""
    reference_host_paths            = 0
    baseline_incompatible_objects   = 0
    restricted_incompatible_objects = 0
    collections                     = []
    objects                         = []
    legacy_controller_objects       = []
  }
  pod_security_host_agent_images = {
    dcgm-exporter = "nvcr.io/nvidia/k8s/dcgm-exporter@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a"
    node-exporter = "quay.io/prometheus/node-exporter:v1.12.1@sha256:8c9bac11973b94b59be88d6e11fee4429aa743c8846cdc75d65b18db33f6a106"
    otel-node     = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-k8s@sha256:3a8f46e1ff33546d36ddd94ef8721c5807718e25825f3e3f6eb5d552fd24e422"
    gpu-observer  = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
  }
  pod_security_reference_tools_files = {
    "placement-contract.json"         = file("${path.module}/../../reference-data/placement-contract.json")
    "reference_data.py"               = file("${path.module}/../../reference-data/reference_data.py")
    "verify_checkpoint_durability.py" = file("${path.module}/../../reference-data/verify_checkpoint_durability.py")
    "verify_csi_readiness.py"         = file("${path.module}/../../reference-data/verify_csi_readiness.py")
  }
  pod_security_reference_tools_sha256 = sha256(jsonencode(local.pod_security_reference_tools_files))
  pod_security_successor_proof_source_files = {
    "verify_checkpoint_durability.py" = file("${path.module}/../../reference-data/verify_checkpoint_durability.py")
    "verify_csi_readiness.py"         = file("${path.module}/../../reference-data/verify_csi_readiness.py")
  }
  pod_security_successor_proof_source_sha256 = sha256(jsonencode(local.pod_security_successor_proof_source_files))
  pod_security_active_proof_generation_id = try(
    var.pod_security_successor_storage.proof_generation_ledger.active_generation,
    strrep("0", 64),
  )
  pod_security_active_proof_generation = try(
    var.pod_security_successor_storage.proof_generation_ledger.generations[local.pod_security_active_proof_generation_id],
    {
      sequence            = 0
      attempt             = 0
      dataset_id          = "prepare"
      dataset_revision    = "prepare"
      dataset_tree_sha256 = strrep("0", 64)
      deployment_nonce    = "prepare"
      probe_image         = "prepare.invalid@sha256:${strrep("0", 64)}"
      tools_data          = local.pod_security_successor_proof_source_files
      tools_data_sha256   = local.pod_security_successor_proof_source_sha256
    },
  )
  reference_data_source_catalog = jsondecode(file("${path.module}/../../reference-data/source-catalog.json"))
  pod_security_receipt_context = {
    cluster_id       = var.cluster_id
    run_id           = var.run_id
    kube_system_uid  = var.kube_system_uid
    deployment_nonce = coalesce(var.pod_security_rollout_receipt.deployment_nonce, "prepare")
    exception_admission_sha256 = sha256(jsonencode({
      host_policy_sha256     = filesha256("${path.module}/../foundation/pod_security_admission.tf")
      snapshot_policy_sha256 = filesha256("${path.module}/../foundation/pod_security_snapshot_admission.tf")
      rollout_manager        = "system:serviceaccount:fs2-system:fs2-pod-security-rollout-manager"
      host_agent_images      = local.pod_security_host_agent_images
      storage_probe_image    = local.pod_security_active_proof_generation.probe_image
      storage_tools_config   = "fs2-reference-data-tools-${substr(local.pod_security_active_proof_generation.tools_data_sha256, 0, 12)}"
      storage_generation     = local.pod_security_active_proof_generation_id
      storage_attempt        = local.pod_security_active_proof_generation.attempt
    }))
    psa_version           = var.pod_security_version
    scientific_namespaces = sort(tolist(var.pod_security_existing_scientific_namespaces))
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
      namespace        = "fs2-reference-data"
      name             = "fs2-reference-data-rwx"
      uid              = local.pod_security_receipt_required ? data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].uid : "prepare"
      resource_version = local.pod_security_receipt_required ? data.kubernetes_persistent_volume_claim_v1.reference_data[0].metadata[0].resource_version : "prepare"
      volume_name      = local.pod_security_receipt_required ? data.kubernetes_persistent_volume_claim_v1.reference_data[0].spec[0].volume_name : "prepare"
      storage_class    = "fs2-reference-data-retained-sc"
    }
    dataset = {
      id          = var.reference_data.pipeline.bundle_id
      revision    = try(local.reference_data_source_catalog.bundles[var.reference_data.pipeline.bundle_id].revision, "prepare")
      tree_sha256 = var.reference_data.expected_tree_sha256 == null ? "" : var.reference_data.expected_tree_sha256
    }
    storage = {
      filesystem_id   = try(var.reference_data.storage_contract.filesystem.id, "prepare")
      capacity_gib    = try(var.reference_data.storage_contract.filesystem.size_gib, 0)
      claim_size_gib  = 1611
      forbid_deletion = try(var.reference_data.storage_contract.filesystem.forbid_deletion, false)
      retention_mode  = try(var.reference_data.storage_contract.lifecycle.retention_mode, "prepare")
    }
    storage_evidence = {
      read_proof_schema       = "fs2-serve.nebius.ai/reference-data-csi-readiness/v2"
      checkpoint_proof_schema = "fs2-serve.nebius.ai/checkpoint-durability-proof/v2"
      probe_image             = coalesce(var.reference_data.status.image, "prepare.invalid@sha256:${strrep("0", 64)}")
      tools_config_map        = var.reference_data.enabled ? "fs2-reference-data-tools-${substr(local.pod_security_reference_tools_sha256, 0, 12)}" : "fs2-reference-data-tools-prepare"
      tools_data_sha256       = var.reference_data.enabled ? local.pod_security_reference_tools_sha256 : strrep("0", 64)
    }
    successor_storage_sha256 = (
      var.pod_security_successor_storage == null ?
      strrep("0", 64) :
      sha256(jsonencode(var.pod_security_successor_storage))
    )
    successor_storage = var.pod_security_successor_storage
    baseline = {
      schema                          = local.pod_security_baseline_artifact.schema
      artifact_sha256                 = local.pod_security_receipt_required ? filesha256(var.pod_security_rollout_receipt.baseline_artifact_path) : ""
      inventory_sha256                = local.pod_security_baseline_artifact.inventory_sha256
      reference_host_paths            = local.pod_security_baseline_artifact.reference_host_paths
      baseline_incompatible_objects   = local.pod_security_baseline_artifact.baseline_incompatible_objects
      restricted_incompatible_objects = local.pod_security_baseline_artifact.restricted_incompatible_objects
    }
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

  consumer_role             = "downstream"
  kubeconfig_path           = var.kubeconfig_path
  kube_context              = var.kube_context
  phase                     = var.pod_security_rollout_phase
  receipt_bundle_path       = var.pod_security_rollout_receipt.bundle_path
  receipt_public_key_path   = var.pod_security_rollout_receipt.public_key_path
  receipt_public_key_sha256 = var.pod_security_rollout_receipt.public_key_sha256
  baseline_artifact_path    = var.pod_security_rollout_receipt.baseline_artifact_path
  cleanup_result_path       = var.pod_security_rollout_receipt.cleanup_result_path
  receipt_key_id            = var.pod_security_rollout_receipt.key_id
  receipt_signer_identity   = var.pod_security_rollout_receipt.signer_identity
  expected_context          = local.pod_security_receipt_context
}

module "pod_security_rollout_ack" {
  source = "../../modules/pod-security-rollout-gate"

  consumer_role             = "downstream"
  action                    = "acknowledge"
  kubeconfig_path           = var.kubeconfig_path
  kube_context              = var.kube_context
  phase                     = var.pod_security_rollout_phase
  receipt_bundle_path       = var.pod_security_rollout_receipt.bundle_path
  receipt_public_key_path   = var.pod_security_rollout_receipt.public_key_path
  receipt_public_key_sha256 = var.pod_security_rollout_receipt.public_key_sha256
  baseline_artifact_path    = var.pod_security_rollout_receipt.baseline_artifact_path
  cleanup_result_path       = var.pod_security_rollout_receipt.cleanup_result_path
  receipt_key_id            = var.pod_security_rollout_receipt.key_id
  receipt_signer_identity   = var.pod_security_rollout_receipt.signer_identity
  expected_context          = local.pod_security_receipt_context

  depends_on = [
    kubernetes_labels.existing_scientific_pod_security,
    kubernetes_config_map_v1.dcgm_cold_config,
    kubernetes_config_map_v1.dcgm_metrics,
    helm_release.dcgm_exporter_exception,
    module.academic_assets,
    module.reference_data,
  ]
}

resource "terraform_data" "pod_security_rollout_contract" {
  input = module.pod_security_rollout_gate.verification

  lifecycle {
    precondition {
      condition = var.pod_security_rollout_phase == "prepare" || (
        var.reference_data.enabled &&
        var.pod_security_successor_storage != null &&
        var.reference_data.storage_contract.lifecycle.retention_mode == "retain" &&
        var.reference_data.storage_contract.filesystem.forbid_deletion &&
        var.reference_data.storage_contract.filesystem.node_mount_path == "/mnt/fs2-reference-data" &&
        var.reference_data.storage_contract.filesystem.host_path == "/mnt/fs2-reference-data/data" &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611
      )
      error_message = "Every post-prepare phase requires the exact retained reference-data filesystem and signed rollout context."
    }
  }
}

# These namespaces are owned by bounded scientific acceptance/deployment
# workflows outside this state. Manage only the six PSA label fields; never
# adopt or replace the Namespace object itself.
resource "kubernetes_labels" "existing_scientific_pod_security" {
  # fs2-academic-poc is created and labeled by the academic-assets module in
  # this same state. Avoid two field managers owning the six PSA label keys.
  for_each = var.pod_security_rollout_phase == "enforce" ? setsubtract(
    var.pod_security_existing_scientific_namespaces,
    toset(["fs2-academic-poc"]),
  ) : []

  api_version = "v1"
  kind        = "Namespace"

  metadata {
    name = each.value
  }

  labels = local.existing_scientific_pod_security_labels
  force  = false

  depends_on = [terraform_data.pod_security_rollout_contract]
}
