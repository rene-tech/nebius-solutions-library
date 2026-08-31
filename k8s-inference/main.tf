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

  }
}
