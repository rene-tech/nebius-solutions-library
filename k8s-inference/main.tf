resource "terraform_data" "deployment_contract" {
  input = local.deployment_contract

  lifecycle {
    precondition {
      condition     = !var.deployment.scientific_batch.writes_enabled || var.deployment.scientific_batch.enabled
      error_message = "Scientific batch writes require the scientific batch controller gate."
    }

    # Kueue compares an excluded prefix literally against the whole
    # ResourceName, so an auxiliary device prefix that also matches an
    # accelerator would silently stop budgeting GPUs.
    precondition {
      condition = try(alltrue([
        for prefix in local.kueue_auxiliary_resource_prefixes : alltrue([
          for resource_name in local.root_accelerator_resource_names :
          !startswith(resource_name, prefix)
        ])
      ]), false)
      error_message = "A ModelExpress auxiliary RDMA resource name would also exclude an accelerator resource from Kueue quota; rename the auxiliary resource so it cannot prefix an accelerator resource this deployment budgets."
    }

    # With core admission on, an auxiliary prefix must not shadow cpu or memory
    # either, because Kueue matches prefixes literally.
    precondition {
      condition = try(
        var.deployment.scheduling.core_capacity == null || alltrue([
          for prefix in local.kueue_auxiliary_resource_prefixes : alltrue([
            for core_name in ["cpu", "memory"] : !startswith(core_name, prefix)
          ])
        ]),
        false,
      )
      error_message = "Core-resource admission is enabled, but an auxiliary resource prefix is also a prefix of cpu or memory, so Kueue would drop core requests before admission and the core quota would be inert."
    }

    precondition {
      condition = try(
        alltrue([
          for queue_name, queue in local.root_scheduling_cluster_queues :
          toset(local.root_scheduling_queue_pool_order[queue_name]) == toset(local.root_scheduling_pool_ids) &&
          length(local.root_scheduling_queue_pool_order[queue_name]) == length(distinct(local.root_scheduling_queue_pool_order[queue_name])) &&
          length(setsubtract(toset(keys(queue.pool_quotas)), toset(local.root_scheduling_pool_ids))) == 0 &&
          alltrue([
            for check in queue.admission_checks :
            length(setsubtract(toset(check.on_flavors), toset(local.root_scheduling_pool_ids))) == 0
          ]) &&
          alltrue([
            for pool_id, quota in queue.pool_quotas :
            floor(quota.nominal_quota) == quota.nominal_quota && quota.nominal_quota >= 0 &&
            (quota.borrowing_limit == null || (
              var.deployment.scheduling.cohort.enabled &&
              floor(quota.borrowing_limit) == quota.borrowing_limit && quota.borrowing_limit >= 0
            )) &&
            (quota.lending_limit == null || (
              var.deployment.scheduling.cohort.enabled &&
              floor(quota.lending_limit) == quota.lending_limit && quota.lending_limit >= 0 &&
              quota.lending_limit <= quota.nominal_quota
            ))
          ])
          ]) && alltrue([
          for pool_id, nominal in local.root_scheduling_nominal_by_pool :
          nominal <= local.root_scheduling_pool_capacity[pool_id]
        ]),
        false,
      )
      error_message = "Root scheduling pool orders must be exact and duplicate-free; quota and AdmissionCheck pool keys must be selected pools; quota/limits must be whole and nonnegative, limits require a cohort, lending cannot exceed nominal, and summed floors cannot exceed effective accelerator capacity."
    }

    precondition {
      condition = try(
        alltrue([
          for queue_name, queue in local.root_scheduling_local_queues :
          (queue_name != local.root_default_local_queue_name || (
            queue.cluster_queue == local.root_default_cluster_queue_name &&
            queue.namespace == "fs2-models"
          )) &&
          contains(local.root_referenceable_cluster_queues, queue.cluster_queue) &&
          contains(
            local.root_scheduling_cluster_queue_namespaces[queue.cluster_queue],
            queue.namespace,
          ) &&
          alltrue([
            for model_id in queue.model_ids :
            length(model_id) <= 63 && can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", model_id))
          ]) &&
          (length(queue.model_ids) == 0 ? (
            length(queue.tenant_ids) == 0 && length(queue.service_classes) == 0
          ) : length(queue.service_classes) > 0) &&
          alltrue([
            for service_class in queue.service_classes :
            contains(keys(var.deployment.scheduling.service_classes), service_class) &&
            contains(keys(local.root_scheduling_cluster_queues), queue.cluster_queue) &&
            (
              length(var.deployment.scheduling.service_classes[service_class].pool_preference) == 0 ?
              local.root_scheduling_pool_ids :
              var.deployment.scheduling.service_classes[service_class].pool_preference
            ) == local.root_scheduling_queue_pool_order[queue.cluster_queue] &&
            (!contains(["platform-critical", "presentation"], service_class) || (
              contains(
                ["LowerPriority", "Any"],
                local.root_scheduling_cluster_queues[queue.cluster_queue].preemption.reclaim_within_cohort,
                ) && contains(
                ["LowerPriority", "LowerOrNewerEqualPriority"],
                local.root_scheduling_cluster_queues[queue.cluster_queue].preemption.within_cluster_queue,
              )
            ))
          ])
        ]) &&
        length(local.root_exact_lane_binding_keys) == length(distinct(local.root_exact_lane_binding_keys)) &&
        length(local.root_wildcard_lane_binding_keys) == length(distinct(local.root_wildcard_lane_binding_keys)),
        false,
      )
      error_message = "Root LocalQueues must preserve the stable fs2-models binding, live in a namespace their ClusterQueue admits, use strict DNS-label model IDs, and define unambiguous service-class/tenant/model routes whose pool order matches the selected ClusterQueue; high-priority routes require both cohort reclaim and same-queue displacement."
    }

    precondition {
      condition = try(
        alltrue([
          for service_class, policy in var.deployment.scheduling.service_classes :
          contains(
            keys(local.root_scheduling_local_queues),
            coalesce(policy.default_local_queue, local.root_default_local_queue_name),
          ) &&
          (
            length(policy.pool_preference) == 0 ? local.root_scheduling_pool_ids : policy.pool_preference
            ) == local.root_scheduling_queue_pool_order[
            local.root_scheduling_local_queues[
              coalesce(policy.default_local_queue, local.root_default_local_queue_name)
            ].cluster_queue
          ] &&
          (!contains(["platform-critical", "presentation"], service_class) || (
            contains(
              ["LowerPriority", "Any"],
              local.root_scheduling_cluster_queues[
                local.root_scheduling_local_queues[
                  coalesce(policy.default_local_queue, local.root_default_local_queue_name)
                ].cluster_queue
              ].preemption.reclaim_within_cohort,
              ) && contains(
              ["LowerPriority", "LowerOrNewerEqualPriority"],
              local.root_scheduling_cluster_queues[
                local.root_scheduling_local_queues[
                  coalesce(policy.default_local_queue, local.root_default_local_queue_name)
                ].cluster_queue
              ].preemption.within_cluster_queue,
            )
          ))
        ]) &&
        alltrue([
          for priorities in values(local.root_service_priority_class_groups) :
          length(distinct(priorities)) == 1
        ]) &&
        alltrue([
          for priority_name, priority in var.deployment.dynamic_models.priority_classes :
          !contains(keys(local.root_service_priority_class_groups), priority_name) ||
          local.root_service_priority_class_groups[priority_name][0] == priority
        ]),
        false,
      )
      error_message = "Every service class must resolve to an existing LocalQueue with an identical ClusterQueue pool order, and shared WorkloadPriorityClass names must agree on one value."
    }

    precondition {
      condition = try(
        # Without a Cohort no queue can borrow residual capacity, so floors must
        # equal capacity exactly rather than stranding accelerators.
        (
          var.deployment.scheduling.cohort.enabled || alltrue([
            for pool_id, nominal in local.root_scheduling_nominal_by_pool :
            nominal == local.root_scheduling_pool_capacity[pool_id]
          ])
        ) &&
        # Kueue orders LocalQueues by decayed fair-share usage before priority,
        # so multi-lane fair-share ordering must be acknowledged explicitly.
        alltrue([
          for queue_name, queue in local.root_scheduling_cluster_queues :
          !(queue.admission_fair_sharing && length(local.root_serving_lanes[queue_name]) > 1) ||
          queue.fair_share_precedence_acknowledged
        ]) &&
        # A namespace-bound model must have a lane in its own namespace for
        # every caller-selectable class, or a caller's class silently resolves
        # to a namespace without its licensed assets.
        alltrue([
          for model_id, namespace in local.root_namespace_bound_models :
          length(setsubtract(
            local.root_caller_selectable_service_classes,
            toset(flatten([
              for queue in values(local.root_scheduling_local_queues) : tolist(queue.service_classes)
              if queue.namespace == namespace && contains(queue.model_ids, model_id)
            ])),
          )) == 0
        ]) &&
        alltrue([
          for queue_name, queue in local.root_scheduling_cluster_queues :
          length(local.root_scheduling_cluster_queue_namespaces[queue_name]) <= 32 &&
          alltrue([
            for namespace in local.root_scheduling_cluster_queue_namespaces[queue_name] :
            length(namespace) <= 63 && can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", namespace))
          ])
        ]),
        false,
      )
      error_message = "Root scheduling preflight failed: with the Cohort disabled nominal floors must equal pool capacity exactly; a ClusterQueue serving more than one lane with usage-based admission fair sharing must set fair_share_precedence_acknowledged because Kueue orders lanes by fair-share usage before WorkloadPriorityClass; a namespace-bound model needs a lane in its own namespace for every caller-selectable class; and admitted namespaces must be at most 32 label-safe names."
    }

    precondition {
      condition = (
        !var.deployment.scheduling.academic_raw_data_stages ||
        (
          local.root_academic_execution_enabled &&
          var.deployment.storage.reference_data.enabled &&
          var.deployment.scheduling.core_capacity != null
        )
      )
      error_message = "deployment.scheduling.academic_raw_data_stages requires enabled academic execution, an enabled reference-data plane, and core_capacity so Kueue counts the cpu and memory a raw stage requests. Leave it false to run licensed models from enriched inputs only."
    }

    # Mirror of the workloads implication, before any stage mutates anything.
    precondition {
      condition = (
        var.deployment.scheduling.core_capacity != null ||
        (
          !var.deployment.storage.reference_data.enabled &&
          length(var.deployment.scheduling.cpu_stage_requests) == 0
        )
      )
      error_message = "Enabling the reference-data plane or declaring a CPU stage request means core-requesting work must actually be admitted, but Kueue drops cpu and memory before admission unless core admission is on. Set deployment.scheduling.core_capacity to the exact aggregate schedulable cpu/memory of the pools backing this Kueue installation."
    }

    # The complete core-admission math, mirrored from the scheduling module so
    # an invalid quota is refused before infrastructure mutates anything.
    precondition {
      condition = try(
        local.root_core_admission_enabled ? (
          floor(var.deployment.scheduling.core_capacity.cpu_millicores) ==
          var.deployment.scheduling.core_capacity.cpu_millicores &&
          var.deployment.scheduling.core_capacity.cpu_millicores >= 1 &&
          floor(var.deployment.scheduling.core_capacity.memory_mib) ==
          var.deployment.scheduling.core_capacity.memory_mib &&
          var.deployment.scheduling.core_capacity.memory_mib >= 1 &&
          length(var.deployment.scheduling.core_capacity.flavor_name) <= 63 &&
          can(regex(
            "^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
            var.deployment.scheduling.core_capacity.flavor_name,
          )) &&
          alltrue([
            for queue_name, queue in local.root_scheduling_cluster_queues :
            floor(queue.core_quota.cpu_millicores) == queue.core_quota.cpu_millicores &&
            queue.core_quota.cpu_millicores >= 0 &&
            floor(queue.core_quota.memory_mib) == queue.core_quota.memory_mib &&
            queue.core_quota.memory_mib >= 0
          ]) &&
          local.root_core_floor_totals.cpu_millicores <=
          var.deployment.scheduling.core_capacity.cpu_millicores &&
          local.root_core_floor_totals.memory_mib <=
          var.deployment.scheduling.core_capacity.memory_mib &&
          (
            var.deployment.scheduling.cohort.enabled || (
              local.root_core_floor_totals.cpu_millicores ==
              var.deployment.scheduling.core_capacity.cpu_millicores &&
              local.root_core_floor_totals.memory_mib ==
              var.deployment.scheduling.core_capacity.memory_mib
            )
          )
          ) : alltrue([
            for queue_name, queue in local.root_scheduling_cluster_queues :
            queue.core_quota.cpu_millicores == 0 && queue.core_quota.memory_mib == 0
        ]),
        false,
      )
      error_message = "Core-resource admission requires exact positive whole aggregate cpu/memory totals, a label-safe core ResourceFlavor name, whole nonnegative per-ClusterQueue floors whose sum never exceeds those totals, and with the Cohort disabled floors that equal them exactly because no queue could borrow the residual; without core_capacity no ClusterQueue may declare a core floor at all, because Kueue drops the request before admission."
    }

    # One Pod must fit one node and the quota of the queue that admits it.
    precondition {
      condition = try(
        alltrue([
          for class_name, request in local.root_cpu_stage_requests :
          floor(request.cpu_millicores) == request.cpu_millicores && request.cpu_millicores >= 1 &&
          floor(request.memory_mib) == request.memory_mib && request.memory_mib >= 1 &&
          # Only the derived reference-data class exists downstream, so an
          # unknown class name must fail here rather than after the
          # infrastructure stage has already run.
          contains(["reference-data"], class_name) &&
          (
            class_name != "reference-data" ? true : (
              local.root_reference_cpu_capacity != null &&
              request.cpu_millicores <= local.root_reference_cpu_capacity.cpu_millicores &&
              request.memory_mib <= local.root_reference_cpu_capacity.memory_mib &&
              local.root_reference_queue_cpu_millicores >= request.cpu_millicores &&
              local.root_reference_queue_memory_mib >= request.memory_mib &&
              # The contract carries integer MiB, so a Ki quota that is not a
              # whole MiB is refused rather than silently truncated.
              floor(local.root_reference_queue_memory_mib) == local.root_reference_queue_memory_mib &&
              floor(local.root_reference_queue_cpu_millicores) == local.root_reference_queue_cpu_millicores
            )
          )
        ]),
        false,
      )
      error_message = "Every CPU stage request must be whole and positive, must fit inside its class's per-node schedulable capacity, and must fit inside the nominal cpu/memory quota of the ClusterQueue that admits it. The raw AlphaFold 3 data stage needs 16000m and 65536Mi, so the reference-data pool must advertise at least that per node and its ClusterQueue must budget at least that much; Kubernetes allocatable is lower than a machine preset's nominal size, so declare measured schedulable capacity rather than the preset name."
    }

    precondition {
      condition     = length(local.root_academic_lane_queue_collisions) == 0
      error_message = "A deployment.scheduling.local_queues entry collides with the derived academic execution LocalQueue name; rename the operator lane or change the academic execution queue so exactly one definition owns it."
    }

    precondition {
      condition = !local.using_custom_accelerator_pools || alltrue([
        for pool in values(var.deployment.accelerator_pools) : pool.mig.strategy == "none"
      ])
      error_message = "Active custom MIG scheduling is blocked at the root until the accelerator contract reports exact advertised resource_units_per_node."
    }

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
      condition = (
        !var.deployment.storage.scientific_artifacts.enabled ||
        can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", local.scientific_artifacts_bucket_name))
      )
      error_message = "The effective scientific-artifact bucket name must be a globally valid 3-63 character object-storage name; set deployment.storage.scientific_artifacts.object_storage.bucket_name explicitly when the derived name is too long."
    }

    precondition {
      condition = (
        !var.deployment.storage.scientific_artifacts.enabled ||
        !var.deployment.storage.reference_data.enabled ||
        local.scientific_artifacts_bucket_name != local.reference_data_bucket_name
      )
      error_message = "The scientific result store must use a bucket distinct from the reference-data plane; the reference-data bucket and key are never reused or widened for results."
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
