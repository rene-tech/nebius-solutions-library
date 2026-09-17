locals {
  pod_security_successor_storage_enabled = var.pod_security_successor_storage != null
  pod_security_reference_successors = {
    "fs2-bioir-boltz2/fs2-reference-data-rwx" = {
      namespace = "fs2-bioir-boltz2"
      claim     = "fs2-reference-data-rwx"
      volume    = "fs2-sai07-ref-bioir-boltz2"
    }
    "fs2-bioir-coverage/fs2-reference-data-rwx" = {
      namespace = "fs2-bioir-coverage"
      claim     = "fs2-reference-data-rwx"
      volume    = "fs2-sai07-ref-bioir-coverage"
    }
    "fs2-bioir-openfold/fs2-reference-data-rwx" = {
      namespace = "fs2-bioir-openfold"
      claim     = "fs2-reference-data-rwx"
      volume    = "fs2-sai07-ref-bioir-openfold"
    }
    "fs2-bioir-protenix/fs2-reference-data-rwx" = {
      namespace = "fs2-bioir-protenix"
      claim     = "fs2-reference-data-rwx"
      volume    = "fs2-sai07-ref-bioir-protenix"
    }
    "fs2-bioir-snapshot/fs2-reference-data-rwx" = {
      namespace = "fs2-bioir-snapshot"
      claim     = "fs2-reference-data-rwx"
      volume    = "fs2-sai07-ref-bioir-snapshot"
    }
    "fs2-snapshot-operations/fs2-snapshot-reference" = {
      namespace = "fs2-snapshot-operations"
      claim     = "fs2-snapshot-reference"
      volume    = "fs2-sai07-ref-snapshot-operations"
    }
  }
  pod_security_successor_namespaces = toset([
    for successor in values(local.pod_security_reference_successors) : successor.namespace
  ])
  pod_security_successor_contract_sha256 = local.pod_security_successor_storage_enabled ? sha256(
    jsonencode(var.pod_security_successor_storage)
  ) : strrep("0", 64)
  pod_security_proof_generations = local.pod_security_successor_storage_enabled ? (
    var.pod_security_successor_storage.proof_generation_ledger.generations
  ) : {}
  # Resource addresses include immutable content or the full proof-generation
  # identity. The signed ledger retains every prior key (maximum eight); adding
  # a nonce/attempt creates new objects and never replaces a protected one.
  pod_security_successor_tool_instances = local.pod_security_successor_storage_enabled ? merge([
    for generation_id, generation in local.pod_security_proof_generations : {
      for namespace in local.pod_security_successor_namespaces :
      "${generation.tools_data_sha256}/${namespace}" => {
        namespace         = namespace
        tools_data        = generation.tools_data
        tools_data_sha256 = generation.tools_data_sha256
      }
    }
  ]...) : {}
  pod_security_reference_probe_instances = local.pod_security_successor_storage_enabled ? merge([
    for generation_id, generation in local.pod_security_proof_generations : {
      for successor_key, successor in local.pod_security_reference_successors :
      "${generation_id}/${successor_key}" => merge(successor, {
        successor_key = successor_key
        generation_id = generation_id
        generation    = generation
      })
    }
  ]...) : {}
  pod_security_checkpoint_probe_instances = local.pod_security_successor_storage_enabled ? merge([
    for generation_id, generation in local.pod_security_proof_generations : {
      for mode in ["write", "read"] :
      "${generation_id}/${mode}" => {
        mode          = mode
        generation_id = generation_id
        generation    = generation
      }
    }
  ]...) : {}
}

# Read the already retained canonical PV immediately before creating any alias.
# The exact UID, resourceVersion, CSI handle, attributes and capacity are an
# operator-supplied custody contract and are also bound into the signed rollout
# context. A same-looking replacement PV therefore cannot be substituted.
data "kubernetes_persistent_volume_v1" "pod_security_reference_source" {
  count = local.pod_security_successor_storage_enabled ? 1 : 0

  metadata {
    name = var.pod_security_successor_storage.reference_source.persistent_volume_name
  }
}

data "kubernetes_namespace_v1" "pod_security_successor" {
  for_each = local.pod_security_successor_storage_enabled ? local.pod_security_successor_namespaces : []

  metadata {
    name = each.value
  }
}

resource "terraform_data" "pod_security_successor_storage_contract" {
  count = local.pod_security_successor_storage_enabled ? 1 : 0

  input = {
    contract_sha256 = local.pod_security_successor_contract_sha256
    source_pv = {
      name             = data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].metadata[0].name
      uid              = data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].metadata[0].uid
      resource_version = data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].metadata[0].resource_version
    }
  }

  lifecycle {
    precondition {
      condition = (
        var.reference_data.enabled &&
        var.reference_data.storage_contract.lifecycle.retention_mode == "retain" &&
        var.reference_data.storage_contract.filesystem.forbid_deletion &&
        var.reference_data.storage_contract.filesystem.size_gib >= var.pod_security_successor_storage.reference_source.capacity_gib &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611 + var.pod_security_successor_storage.checkpoint_source.capacity_gib &&
        data.kubernetes_persistent_volume_claim_v1.reference_data[0].spec[0].volume_name == var.pod_security_successor_storage.reference_source.persistent_volume_name &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].metadata[0].uid == var.pod_security_successor_storage.reference_source.uid &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].metadata[0].resource_version == var.pod_security_successor_storage.reference_source.resource_version &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].storage_class_name == "fs2-reference-data-retained-sc" &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].persistent_volume_reclaim_policy == "Retain" &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].capacity["storage"] == var.pod_security_successor_storage.reference_source.capacity_quantity &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].persistent_volume_source[0].csi[0].driver == var.pod_security_successor_storage.reference_source.csi_driver &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].persistent_volume_source[0].csi[0].volume_handle == var.pod_security_successor_storage.reference_source.volume_handle &&
        data.kubernetes_persistent_volume_v1.pod_security_reference_source[0].spec[0].persistent_volume_source[0].csi[0].volume_attributes == var.pod_security_successor_storage.reference_source.volume_attributes
      )
      error_message = "The live canonical reference PV must exactly match the signed, independently receipted retained CSI source before aliases are materialized."
    }
    precondition {
      condition = (
        var.reference_data.expected_tree_sha256 != null &&
        var.reference_data.status.image != null &&
        var.pod_security_rollout_receipt.deployment_nonce != null &&
        can(regex("^[^@[:space:]]+@sha256:[a-f0-9]{64}$", var.reference_data.status.image)) &&
        local.pod_security_active_proof_generation_id == var.pod_security_successor_storage.proof_generation_ledger.active_generation &&
        local.pod_security_active_proof_generation.dataset_id == var.reference_data.pipeline.bundle_id &&
        local.pod_security_active_proof_generation.dataset_revision == local.reference_data_source_catalog.bundles[var.reference_data.pipeline.bundle_id].revision &&
        local.pod_security_active_proof_generation.dataset_tree_sha256 == var.reference_data.expected_tree_sha256 &&
        local.pod_security_active_proof_generation.probe_image == var.reference_data.status.image &&
        local.pod_security_active_proof_generation.tools_data == local.pod_security_successor_proof_source_files &&
        local.pod_security_active_proof_generation.tools_data_sha256 == local.pod_security_successor_proof_source_sha256 &&
        alltrue([
          for generation_id, generation in local.pod_security_proof_generations :
          generation_id == sha256(jsonencode(generation)) &&
          generation.tools_data_sha256 == sha256(jsonencode(generation.tools_data))
        ])
      )
      error_message = "Successor provisioning requires an exact active source generation plus a self-consistent bounded signed history; retries append a new nonce/attempt generation rather than replacing retained objects."
    }
  }
}

# Six fixed, cluster-scoped PV aliases expose the exact existing reference-data
# CSI volume read-only. They do not allocate six empty dynamic directories or
# copy the 1.6 TiB dataset. The CSI handle and attributes are live-bound above.
resource "kubernetes_persistent_volume_v1" "pod_security_reference_successor" {
  for_each = local.pod_security_successor_storage_enabled ? local.pod_security_reference_successors : {}

  metadata {
    name = each.value.volume
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "reference-data-successor"
    })
    annotations = {
      "security.fs2.nebius.ai/custody-contract-sha256"     = local.pod_security_successor_contract_sha256
      "security.fs2.nebius.ai/provisioning-receipt-sha256" = var.pod_security_successor_storage.reference_source.provisioning_receipt_sha256
      "security.fs2.nebius.ai/storage-owner"               = var.pod_security_successor_storage.reference_source.storage_owner
    }
  }

  spec {
    access_modes                     = ["ReadOnlyMany"]
    capacity                         = { storage = "${var.pod_security_successor_storage.reference_source.capacity_gib}Gi" }
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = "fs2-reference-data-retained-sc"
    volume_mode                      = "Filesystem"

    claim_ref {
      api_version = "v1"
      kind        = "PersistentVolumeClaim"
      name        = each.value.claim
      namespace   = each.value.namespace
    }

    persistent_volume_source {
      csi {
        driver            = var.pod_security_successor_storage.reference_source.csi_driver
        read_only         = true
        volume_attributes = var.pod_security_successor_storage.reference_source.volume_attributes
        volume_handle     = var.pod_security_successor_storage.reference_source.volume_handle
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.pod_security_successor_storage_contract]
}

resource "kubernetes_persistent_volume_claim_v1" "pod_security_reference_successor" {
  for_each = local.pod_security_successor_storage_enabled ? local.pod_security_reference_successors : {}

  metadata {
    name      = each.value.claim
    namespace = data.kubernetes_namespace_v1.pod_security_successor[each.value.namespace].metadata[0].name
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "reference-data-successor"
    })
    annotations = {
      "security.fs2.nebius.ai/content-tree-sha256"     = var.reference_data.expected_tree_sha256
      "security.fs2.nebius.ai/custody-contract-sha256" = local.pod_security_successor_contract_sha256
    }
  }

  spec {
    access_modes       = ["ReadOnlyMany"]
    storage_class_name = "fs2-reference-data-retained-sc"
    volume_mode        = "Filesystem"
    volume_name        = kubernetes_persistent_volume_v1.pod_security_reference_successor[each.key].metadata[0].name
    resources {
      requests = { storage = "${var.pod_security_successor_storage.reference_source.capacity_gib}Gi" }
    }
  }

  wait_until_bound = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_storage_class_v1" "pod_security_snapshot_checkpoints" {
  count = local.pod_security_successor_storage_enabled ? 1 : 0

  metadata {
    name = "fs2-snapshot-checkpoints-retained-sc"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "snapshot-checkpoint-storage"
    })
    annotations = {
      "security.fs2.nebius.ai/custody-contract-sha256" = local.pod_security_successor_contract_sha256
    }
  }

  storage_provisioner = var.pod_security_successor_storage.checkpoint_source.csi_driver
  reclaim_policy      = "Retain"
  volume_binding_mode = "Immediate"

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_persistent_volume_v1" "pod_security_snapshot_checkpoints" {
  count = local.pod_security_successor_storage_enabled ? 1 : 0

  metadata {
    name = var.pod_security_successor_storage.checkpoint_source.persistent_volume_name
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "snapshot-checkpoint-storage"
    })
    annotations = {
      "security.fs2.nebius.ai/custody-contract-sha256"     = local.pod_security_successor_contract_sha256
      "security.fs2.nebius.ai/provisioning-receipt-sha256" = var.pod_security_successor_storage.checkpoint_source.provisioning_receipt_sha256
      "security.fs2.nebius.ai/storage-owner"               = var.pod_security_successor_storage.checkpoint_source.storage_owner
    }
  }

  spec {
    access_modes                     = ["ReadWriteMany"]
    capacity                         = { storage = "${var.pod_security_successor_storage.checkpoint_source.capacity_gib}Gi" }
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = kubernetes_storage_class_v1.pod_security_snapshot_checkpoints[0].metadata[0].name
    volume_mode                      = "Filesystem"

    claim_ref {
      api_version = "v1"
      kind        = "PersistentVolumeClaim"
      name        = "fs2-snapshot-checkpoints"
      namespace   = "fs2-snapshot-operations"
    }

    persistent_volume_source {
      csi {
        driver            = var.pod_security_successor_storage.checkpoint_source.csi_driver
        read_only         = false
        volume_attributes = var.pod_security_successor_storage.checkpoint_source.volume_attributes
        volume_handle     = var.pod_security_successor_storage.checkpoint_source.volume_handle
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.pod_security_successor_storage_contract]
}

resource "kubernetes_persistent_volume_claim_v1" "pod_security_snapshot_checkpoints" {
  count = local.pod_security_successor_storage_enabled ? 1 : 0

  metadata {
    name      = "fs2-snapshot-checkpoints"
    namespace = data.kubernetes_namespace_v1.pod_security_successor["fs2-snapshot-operations"].metadata[0].name
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "snapshot-checkpoint-storage"
    })
    annotations = {
      "security.fs2.nebius.ai/custody-contract-sha256" = local.pod_security_successor_contract_sha256
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = kubernetes_storage_class_v1.pod_security_snapshot_checkpoints[0].metadata[0].name
    volume_mode        = "Filesystem"
    volume_name        = kubernetes_persistent_volume_v1.pod_security_snapshot_checkpoints[0].metadata[0].name
    resources {
      requests = { storage = "${var.pod_security_successor_storage.checkpoint_source.requested_gib}Gi" }
    }
  }

  wait_until_bound = true

  lifecycle {
    prevent_destroy = true
  }
}

# Replicate the exact content-addressed tooling into every consumer namespace.
# The ConfigMaps are immutable and destruction-protected; a source generation
# change must be introduced as a separately named additive generation.
resource "kubernetes_config_map_v1" "pod_security_successor_tools" {
  for_each = local.pod_security_successor_tool_instances

  metadata {
    name      = "fs2-reference-data-tools-${substr(each.value.tools_data_sha256, 0, 12)}"
    namespace = data.kubernetes_namespace_v1.pod_security_successor[each.value.namespace].metadata[0].name
    labels    = local.common_labels
    annotations = {
      "reference-data.fs2.nebius.ai/source-sha256" = each.value.tools_data_sha256
    }
  }

  immutable = true
  data      = each.value.tools_data

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_job_v1" "pod_security_reference_successor_probe" {
  for_each            = local.pod_security_reference_probe_instances
  wait_for_completion = true

  metadata {
    name      = "${each.value.claim}-read-probe-${substr(each.value.generation_id, 0, 12)}"
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "reference-data-successor-read-probe"
    })
    annotations = {
      "reference-data.fs2.nebius.ai/tree-sha256"          = each.value.generation.dataset_tree_sha256
      "reference-data.fs2.nebius.ai/receipt"              = "receipts/${each.value.generation.dataset_id}/${each.value.generation.dataset_revision}.json"
      "reference-data.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].uid
      "reference-data.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].resource_version
      "reference-data.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].spec[0].volume_name
      "reference-data.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
      "security.fs2.nebius.ai/proof-generation"           = each.value.generation_id
      "security.fs2.nebius.ai/proof-attempt"              = tostring(each.value.generation.attempt)
      "security.fs2.nebius.ai/verified-tree-sha256"       = each.value.generation.dataset_tree_sha256
    }
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 900
    completions             = 1
    parallelism             = 1
    manual_selector         = false

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "reference-data-successor-read-probe"
        })
        annotations = {
          "reference-data.fs2.nebius.ai/tree-sha256"          = each.value.generation.dataset_tree_sha256
          "reference-data.fs2.nebius.ai/receipt"              = "receipts/${each.value.generation.dataset_id}/${each.value.generation.dataset_revision}.json"
          "reference-data.fs2.nebius.ai/bundle"               = each.value.generation.dataset_id
          "reference-data.fs2.nebius.ai/revision"             = each.value.generation.dataset_revision
          "reference-data.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].uid
          "reference-data.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].resource_version
          "reference-data.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].spec[0].volume_name
          "reference-data.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
          "security.fs2.nebius.ai/proof-generation"           = each.value.generation_id
          "security.fs2.nebius.ai/proof-attempt"              = tostring(each.value.generation.attempt)
          "security.fs2.nebius.ai/verified-tree-sha256"       = each.value.generation.dataset_tree_sha256
        }
      }
      spec {
        restart_policy                  = "Never"
        service_account_name            = "default"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector                   = var.reference_data.storage_contract.cpu_pool.node_labels

        toleration {
          key      = var.reference_data.storage_contract.cpu_pool.taint.key
          operator = "Equal"
          value    = var.reference_data.storage_contract.cpu_pool.taint.value
          effect   = var.reference_data.storage_contract.cpu_pool.taint.effect
        }

        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name              = "read-probe"
          image             = each.value.generation.probe_image
          image_pull_policy = "IfNotPresent"
          command = [
            "python", "/opt/fs2/reference-data/verify_csi_readiness.py",
            "--root", "/reference-data",
            "--receipt", "receipts/${each.value.generation.dataset_id}/${each.value.generation.dataset_revision}.json",
            "--bundle", each.value.generation.dataset_id,
            "--revision", each.value.generation.dataset_revision,
            "--tree-sha256", each.value.generation.dataset_tree_sha256,
            "--pvc-uid", kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].uid,
            "--pvc-resource-version", kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].metadata[0].resource_version,
            "--volume-name", kubernetes_persistent_volume_claim_v1.pod_security_reference_successor[each.value.successor_key].spec[0].volume_name,
            "--challenge", each.value.generation.deployment_nonce,
            "--generation", each.value.generation_id,
            "--attempt", tostring(each.value.generation.attempt),
            "--proof-output", "/dev/termination-log",
          ]
          termination_message_path   = "/dev/termination-log"
          termination_message_policy = "File"

          resources {
            requests = { cpu = "50m", memory = "64Mi", "ephemeral-storage" = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi", "ephemeral-storage" = "256Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }

          volume_mount {
            name       = "reference-data"
            mount_path = "/reference-data"
            read_only  = true
          }
          volume_mount {
            name       = "tools"
            mount_path = "/opt/fs2/reference-data"
            read_only  = true
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }

        volume {
          name = "reference-data"
          persistent_volume_claim {
            claim_name = each.value.claim
            read_only  = true
          }
        }
        volume {
          name = "tools"
          config_map {
            name         = kubernetes_config_map_v1.pod_security_successor_tools["${each.value.generation.tools_data_sha256}/${each.value.namespace}"].metadata[0].name
            default_mode = "0555"
          }
        }
        volume {
          name = "tmp"
          empty_dir { size_limit = "64Mi" }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_persistent_volume_claim_v1.pod_security_reference_successor,
    kubernetes_config_map_v1.pod_security_successor_tools,
  ]
}

resource "kubernetes_job_v1" "pod_security_snapshot_checkpoint_write" {
  for_each = {
    for generation_key, generation in local.pod_security_checkpoint_probe_instances :
    generation_key => generation if generation.mode == "write"
  }
  wait_for_completion = true

  metadata {
    name      = "fs2-snapshot-checkpoints-durability-write-${substr(each.value.generation_id, 0, 12)}"
    namespace = "fs2-snapshot-operations"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "snapshot-checkpoint-durability"
    })
    annotations = {
      "security.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid
      "security.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version
      "security.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name
      "security.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
      "security.fs2.nebius.ai/proof-generation"     = each.value.generation_id
      "security.fs2.nebius.ai/proof-attempt"        = tostring(each.value.generation.attempt)
      "security.fs2.nebius.ai/proof-mode"           = "write"
    }
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 900
    completions             = 1
    parallelism             = 1
    manual_selector         = false
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "snapshot-checkpoint-durability"
        })
        annotations = {
          "security.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid
          "security.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version
          "security.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name
          "security.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
          "security.fs2.nebius.ai/proof-generation"     = each.value.generation_id
          "security.fs2.nebius.ai/proof-attempt"        = tostring(each.value.generation.attempt)
          "security.fs2.nebius.ai/proof-mode"           = "write"
        }
      }
      spec {
        restart_policy                  = "Never"
        service_account_name            = "default"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector                   = var.reference_data.storage_contract.cpu_pool.node_labels
        toleration {
          key      = var.reference_data.storage_contract.cpu_pool.taint.key
          operator = "Equal"
          value    = var.reference_data.storage_contract.cpu_pool.taint.value
          effect   = var.reference_data.storage_contract.cpu_pool.taint.effect
        }
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "durability-proof"
          image             = each.value.generation.probe_image
          image_pull_policy = "IfNotPresent"
          command = [
            "python", "/opt/fs2/reference-data/verify_checkpoint_durability.py", "write",
            "--root", "/checkpoints",
            "--pvc-uid", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid,
            "--pvc-resource-version", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version,
            "--volume-name", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name,
            "--challenge", each.value.generation.deployment_nonce,
            "--generation", each.value.generation_id,
            "--attempt", tostring(each.value.generation.attempt),
            "--proof-output", "/dev/termination-log",
          ]
          termination_message_path   = "/dev/termination-log"
          termination_message_policy = "File"
          resources {
            requests = { cpu = "50m", memory = "64Mi", "ephemeral-storage" = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi", "ephemeral-storage" = "256Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "checkpoints"
            mount_path = "/checkpoints"
            read_only  = false
          }
          volume_mount {
            name       = "tools"
            mount_path = "/opt/fs2/reference-data"
            read_only  = true
          }
        }
        volume {
          name = "checkpoints"
          persistent_volume_claim {
            claim_name = "fs2-snapshot-checkpoints"
            read_only  = false
          }
        }
        volume {
          name = "tools"
          config_map {
            name         = kubernetes_config_map_v1.pod_security_successor_tools["${each.value.generation.tools_data_sha256}/fs2-snapshot-operations"].metadata[0].name
            default_mode = "0555"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_job_v1" "pod_security_snapshot_checkpoint_read" {
  for_each = {
    for generation_key, generation in local.pod_security_checkpoint_probe_instances :
    generation_key => generation if generation.mode == "read"
  }
  wait_for_completion = true

  metadata {
    name      = "fs2-snapshot-checkpoints-durability-read-${substr(each.value.generation_id, 0, 12)}"
    namespace = "fs2-snapshot-operations"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "snapshot-checkpoint-durability"
    })
    annotations = {
      "security.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid
      "security.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version
      "security.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name
      "security.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
      "security.fs2.nebius.ai/proof-generation"     = each.value.generation_id
      "security.fs2.nebius.ai/proof-attempt"        = tostring(each.value.generation.attempt)
      "security.fs2.nebius.ai/proof-mode"           = "read"
    }
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 900
    completions             = 1
    parallelism             = 1
    manual_selector         = false
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "snapshot-checkpoint-durability"
        })
        annotations = {
          "security.fs2.nebius.ai/pvc-uid"              = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid
          "security.fs2.nebius.ai/pvc-resource-version" = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version
          "security.fs2.nebius.ai/volume-name"          = kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name
          "security.fs2.nebius.ai/proof-challenge"      = each.value.generation.deployment_nonce
          "security.fs2.nebius.ai/proof-generation"     = each.value.generation_id
          "security.fs2.nebius.ai/proof-attempt"        = tostring(each.value.generation.attempt)
          "security.fs2.nebius.ai/proof-mode"           = "read"
        }
      }
      spec {
        restart_policy                  = "Never"
        service_account_name            = "default"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector                   = var.reference_data.storage_contract.cpu_pool.node_labels
        toleration {
          key      = var.reference_data.storage_contract.cpu_pool.taint.key
          operator = "Equal"
          value    = var.reference_data.storage_contract.cpu_pool.taint.value
          effect   = var.reference_data.storage_contract.cpu_pool.taint.effect
        }
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "durability-proof"
          image             = each.value.generation.probe_image
          image_pull_policy = "IfNotPresent"
          command = [
            "python", "/opt/fs2/reference-data/verify_checkpoint_durability.py", "read",
            "--root", "/checkpoints",
            "--pvc-uid", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].uid,
            "--pvc-resource-version", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].metadata[0].resource_version,
            "--volume-name", kubernetes_persistent_volume_claim_v1.pod_security_snapshot_checkpoints[0].spec[0].volume_name,
            "--challenge", each.value.generation.deployment_nonce,
            "--generation", each.value.generation_id,
            "--attempt", tostring(each.value.generation.attempt),
            "--proof-output", "/dev/termination-log",
          ]
          termination_message_path   = "/dev/termination-log"
          termination_message_policy = "File"
          resources {
            requests = { cpu = "50m", memory = "64Mi", "ephemeral-storage" = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi", "ephemeral-storage" = "256Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "checkpoints"
            mount_path = "/checkpoints"
            read_only  = true
          }
          volume_mount {
            name       = "tools"
            mount_path = "/opt/fs2/reference-data"
            read_only  = true
          }
        }
        volume {
          name = "checkpoints"
          persistent_volume_claim {
            claim_name = "fs2-snapshot-checkpoints"
            read_only  = true
          }
        }
        volume {
          name = "tools"
          config_map {
            name         = kubernetes_config_map_v1.pod_security_successor_tools["${each.value.generation.tools_data_sha256}/fs2-snapshot-operations"].metadata[0].name
            default_mode = "0555"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_job_v1.pod_security_snapshot_checkpoint_write]
}
