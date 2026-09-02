resource "terraform_data" "deployment_contract" {
  input = local.deployment_contract

  lifecycle {
    precondition {
      condition = (
        local.using_custom_accelerator_pools ||
        (
          local.selected_pool_profile.enabled &&
          local.selected_pool_profile.state == "hardware-validated"
        )
      )
      error_message = "The selected accelerator-pool profile is not enabled and hardware-validated. Declaration-only heterogeneous examples cannot reach a cloud plan."
    }

    precondition {
      condition = local.using_custom_accelerator_pools || alltrue([
        for pool_id in keys(local.selected_pool_profile.pools) : try(
          jsondecode(file("${path.module}/catalog/profiles/accelerator-pools.json")).pool_templates[pool_id].enabled &&
          jsondecode(file("${path.module}/catalog/profiles/accelerator-pools.json")).pool_templates[pool_id].state == "hardware-validated" &&
          length([
            for availability in jsondecode(file("${path.module}/catalog/profiles/accelerator-pools.json")).pool_templates[pool_id].region_availability : availability
            if availability.region == var.deployment.target.region &&
            availability.state == "hardware-validated"
          ]) == 1,
          false,
        )
      ])
      error_message = "At least one selected accelerator pool lacks an enabled, hardware-validated binding for the target region."
    }

    precondition {
      condition = alltrue([
        for model_id in local.selected_model_ids : contains(
          keys(local.model_profile_contract.model_autoscaling_targets),
          model_id,
        )
      ])
      error_message = "Every selected model must have an exact rendered deployment and autoscaling target binding."
    }

    precondition {
      condition = (
        var.deployment.artifacts.registry_policy.mode != "regional-mirror" ||
        alltrue([
          for image in values(local.effective_model_images) :
          can(regex("@sha256:[0-9a-f]{64}$", image))
        ])
      )
      error_message = "Regional mirroring requires each effective selected-model image (tfvars override or runtime catalog default) to be digest pinned."
    }

    precondition {
      condition = alltrue([
        for model_id, placement in local.selected_model_placements : try(
          length(setintersection(
            toset(placement.compatible_pool_ids),
            toset(keys(local.effective_pool_capacities)),
          )) > 0,
          false,
        )
      ])
      error_message = "At least one selected model has no qualified placement on the selected accelerator-pool profile."
    }

    precondition {
      condition = alltrue([
        for model_id, placement in local.selected_model_placements : try(
          alltrue([
            for pool_id in placement.compatible_pool_ids :
            placement.gpu_request <= local.effective_pool_facts[pool_id].gpus_per_node
          ]),
          false,
        )
      ])
      error_message = "A selected model requests more GPUs than its tfvars-selected accelerator pool provides."
    }

    precondition {
      condition     = length(local.scale_from_zero_ephemeral_storage_violations) == 0
      error_message = "A selected model cannot trigger its zero-node pool because the Nebius managed autoscaler derives synthetic ephemeral capacity from the boot disk, before local NVMe exists. Increase deployment.accelerator_pools.<pool>.boot_disk.size_gib or keep a node hot. ${join("; ", local.scale_from_zero_ephemeral_storage_violations)}."
    }

    precondition {
      condition = alltrue([
        for model_id in var.deployment.models.scaling.hot : anytrue([
          for pool_id in local.selected_model_placements[model_id].compatible_pool_ids :
          try(local.effective_pool_capacities[pool_id].max_nodes > 0, false)
        ])
      ])
      error_message = "Every hot model requires positive maximum capacity in at least one compatible selected pool."
    }

    precondition {
      condition = (
        !var.deployment.observability.grafana.publish_external ||
        var.deployment.edge.mode == "public"
      )
      error_message = "External Grafana publication requires public edge mode; its exact origin and allowed host are derived from the infrastructure output."
    }

    precondition {
      condition = alltrue([
        for model_id, scaling in var.deployment.models.scaling.overrides :
        scaling.max_replicas <= local.selected_model_replica_ceilings[model_id]
      ])
      error_message = "A model scaling override exceeds the maximum replicas supported by its compatible selected accelerator pools and configured node ceilings."
    }

    precondition {
      condition = (
        !var.deployment.storage.reference_data.enabled ||
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", local.reference_data_bucket_name))
      )
      error_message = "The effective reference-data bucket name must be a globally valid 3-63 character object-storage name; set deployment.storage.reference_data.object_storage.bucket_name explicitly when the derived name is too long."
    }

    precondition {
      condition = alltrue([
        for pool in values(var.deployment.accelerator_pools) :
        !pool.reference_data_filesystem || var.deployment.storage.reference_data.enabled
      ])
      error_message = "An accelerator pool can opt into the reference-data filesystem only when storage.reference_data.enabled=true."
    }

    precondition {
      condition = !var.deployment.storage.reference_data.enabled || (
        (!var.deployment.storage.reference_data.pipeline.enabled || (
          local.reference_data_pipeline_cpu_millicores <= var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.cpu_millicores &&
          local.reference_data_pipeline_memory_mib <= var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.memory_mib &&
          local.reference_data_pipeline_ephemeral_mib <= var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib &&
          local.reference_data_pipeline_cpu_millicores <= local.reference_data_queue_cpu_millicores &&
          local.reference_data_pipeline_memory_mib <= local.reference_data_queue_memory_mib
        )) &&
        local.reference_data_queue_cpu_millicores <= local.reference_data_total_schedulable_capacity.cpu_millicores &&
        local.reference_data_queue_memory_mib <= local.reference_data_total_schedulable_capacity.memory_mib &&
        local.reference_data_required_capacity.cpu_millicores <= local.reference_data_total_schedulable_capacity.cpu_millicores &&
        local.reference_data_required_capacity.memory_mib <= local.reference_data_total_schedulable_capacity.memory_mib &&
        local.reference_data_required_capacity.ephemeral_storage_mib <= local.reference_data_total_schedulable_capacity.ephemeral_storage_mib
      )
      error_message = "Reference-data staging/status requests and Kueue quotas must fit the conservative schedulable capacity of the dedicated tainted CPU preprocessing pool; the Kubernetes system pool is never fallback capacity."
    }

  }
}
resource "kubernetes_namespace_v1" "academic_assets" {
  count = var.academic_assets.enabled ? 1 : 0
  metadata { name = var.academic_assets.namespace }
}

resource "kubernetes_persistent_volume_claim_v1" "academic_assets_runtime" {
  count = var.academic_assets.enabled ? 1 : 0
  metadata {
    name      = var.academic_assets.pvc_name
    namespace = var.academic_assets.namespace
    labels = {
      "fs2.nebius.ai/tenant-id"        = var.academic_assets.tenant_id
      "fs2.nebius.ai/institution-id"   = var.academic_assets.institution_id
      "fs2.nebius.ai/academic-runtime" = "true"
    }
  }
  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = "csi-mounted-fs-path-sc"
    resources { requests = { storage = "${var.academic_assets.storage_gib}Gi" } }
  }
  depends_on = [kubernetes_namespace_v1.academic_assets]
}
