locals {
  reference_data_queue_additional_namespaces = sort(distinct(compact([
    local.academic_cpu_lane_enabled ? var.academic_assets.namespace : "",
    local.model_reference_cpu_lane_enabled ? var.scientific_batch.namespace : "",
  ])))
}

module "reference_data" {
  count  = var.reference_data.enabled ? 1 : 0
  source = "../../reference-data/terraform"

  cluster_region        = try(var.reference_data.storage_contract.region, local.selected_target.region)
  object_storage_region = try(var.reference_data.storage_contract.region, local.selected_target.region)
  object_bucket_name    = try(var.reference_data.storage_contract.object_storage.name, "disabled-reference-data.invalid")
  object_storage_access = coalesce(var.reference_data.object_storage_access, {
    access_key_id       = "DISABLED0"
    secret_reference_id = "mysteryboxsecret-disabled"
    revision            = 1
  })

  namespace                   = var.reference_data.namespace
  shared_filesystem_host_path = try(var.reference_data.storage_contract.filesystem.host_path, "/mnt/fs2-reference-data/data")
  pod_security_rollout_phase  = var.pod_security_rollout_phase
  pod_security_version        = var.pod_security_version
  filesystem_claim = {
    name          = "fs2-reference-data-rwx"
    storage_class = "fs2-reference-data-retained-sc"
    # The request is the measured minimum dataset capacity and may not exceed
    # the exact retained filesystem advertised by infrastructure.
    size_gib     = 1611
    capacity_gib = try(var.reference_data.storage_contract.filesystem.size_gib, 0)
  }
  pod_security_rollout_verification = module.pod_security_rollout_gate.verification
  cpu_pool                          = var.reference_data.storage_contract.cpu_pool
  # The reference CPU ClusterQueue must admit every namespace to which this
  # stage publishes a LocalQueue. That includes both licensed raw-data stages
  # and model-owned preprocessing stages in the scientific workload namespace.
  # Its own owner renders the closed namespace selector from this exact list.
  queue = merge(var.reference_data.queue, {
    additional_namespaces = local.reference_data_queue_additional_namespaces
  })

  object_storage_egress_fqdns = [
    trimsuffix(trimprefix(try(var.reference_data.storage_contract.object_storage.endpoint, "https://storage.${local.selected_target.region}.nebius.cloud"), "https://"), "/"),
  ]
  allow_public_source_staging = var.reference_data.network.allow_public_source_staging
  allow_public_msa_opt_in     = var.reference_data.network.allow_public_msa_opt_in

  status = {
    enabled  = var.reference_data.status.enabled
    image    = var.reference_data.status.image
    replicas = var.reference_data.status.replicas
  }
  service_monitor_enabled = var.reference_data.status.service_monitor_enabled
  pipeline                = var.reference_data.pipeline
  preprocess              = var.reference_data.preprocess

  depends_on = [
    helm_release.reference_data_csi,
    terraform_data.pod_security_rollout_contract,
  ]
}

resource "kubernetes_config_map_v1" "reference_data_retained_context" {
  count = var.reference_data.enabled ? 1 : 0

  metadata {
    name      = "fs2-reference-data-retained-context"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  immutable = true
  data = {
    "context.json" = jsonencode({
      pvc = module.reference_data[0].retained_claim_context
      storage = {
        filesystem_id   = var.reference_data.storage_contract.filesystem.id
        capacity_gib    = var.reference_data.storage_contract.filesystem.size_gib
        claim_size_gib  = module.reference_data[0].retained_claim_context.requested_gib
        forbid_deletion = var.reference_data.storage_contract.filesystem.forbid_deletion
        retention_mode  = var.reference_data.storage_contract.lifecycle.retention_mode
      }
    })
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.pod_security_rollout_contract]
}

resource "terraform_data" "reference_data_contract" {
  count = var.reference_data.enabled ? 1 : 0
  input = {
    storage = var.reference_data.storage_contract
    plane   = module.reference_data[0].dynamic_configuration
  }

  lifecycle {
    precondition {
      condition = !var.reference_data.enabled || (
        var.reference_data.storage_contract.project_id == nonsensitive(var.project_id) &&
        var.reference_data.storage_contract.region == local.selected_target.region &&
        var.reference_data.namespace == "fs2-reference-data" &&
        var.reference_data.storage_contract.cpu_pool.capacity == "regular" &&
        var.reference_data.storage_contract.cpu_pool.node_labels["capacity.fs2.nebius/pool"] == "reference-data" &&
        var.reference_data.storage_contract.cpu_pool.taint.effect == "NoSchedule" &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611 &&
        var.reference_data.storage_contract.filesystem.forbid_deletion &&
        var.reference_data.storage_contract.lifecycle.retention_mode == "retain" &&
        var.reference_data.storage_contract.object_storage.max_size_gib >= 1611 &&
        var.reference_data.storage_contract.object_storage.versioning_policy == "ENABLED" &&
        !var.reference_data.storage_contract.public_msa_default
      )
      error_message = "reference data must use the dedicated namespace and storage-attached regular CPU pool plus the infrastructure-owned same-project/same-region storage contract with at least 1 TiB headroom and private MSA defaults."
    }
  }
}
