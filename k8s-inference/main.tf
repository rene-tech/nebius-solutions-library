resource "terraform_data" "deployment_contract" {
  input = local.deployment_contract

  lifecycle {
    precondition {
      condition = !var.academic_assets.enabled || (
        local.committed_academic_asset_readiness.schema == "fs2-serve.nebius.ai/academic-asset-readiness/v1" &&
        length(local.committed_academic_asset_readiness_sha256) == 64
      )
      error_message = "Enabled academic assets require the checked-in schema-v1 readiness projection and its exact SHA-256 digest."
    }

    precondition {
      condition     = !var.deployment.scientific_batch.writes_enabled || var.deployment.scientific_batch.enabled
      error_message = "Scientific batch writes require the scientific batch controller gate."
    }

    precondition {
      condition = !var.deployment.scientific_batch.enabled || try(
        local.scientific_execution_map.schema == "fs2-serve.nebius.ai/scientific-execution-map/v3" &&
        length(local.scientific_execution_map.models) > 0 &&
        length(local.scientific_execution_map.models) == length(distinct([
          for model in local.scientific_execution_map.models : model.model_id
        ])),
        false,
      )
      error_message = "Enabled scientific batch requires a non-empty schema-v3 execution map with one unique entry per model. Omit deployment.scientific_batch.execution_map to use the committed generated map."
    }

    # A map is one qualified unit: Helm hashes its exact compact JSON bytes and
    # every included profile binds that whole digest. Checking the same bytes at
    # the facade prevents a plausible-looking override from reaching the stage
    # with stale profile or execution identities.
    precondition {
      condition = !var.deployment.scientific_batch.enabled || try(alltrue([
        for model in local.scientific_execution_map.models :
        local.scientific_workload_profiles_by_model_id[model.model_id].qualification.execution_map_sha256 == local.scientific_execution_map_sha256 &&
        local.scientific_workload_profiles_by_model_id[model.model_id].execution_identity.execution_identity_sha256 == model.execution_identity_sha256
      ]), false)
      error_message = "The effective scientific execution map does not match the committed workload-profile qualification digest and execution identities. Regenerate and review the map and profiles together; do not paste or edit generated map fields independently."
    }

    precondition {
      condition = try(
        !var.deployment.scientific_batch.enabled || (
          (length(local.scientific_runtime_cache_mounts) == 0 || var.deployment.scientific_batch.runtime_cache.enabled) &&
          (!var.deployment.scientific_batch.runtime_cache.enabled || length(local.scientific_runtime_cache_mounts) > 0) &&
          alltrue([
            for mount in local.scientific_runtime_cache_mounts :
            mount.claim_name == "fs2-scientific-runtime-cache" &&
            mount.host_path == null &&
            mount.mount_path == "/cache" &&
            mount.sub_path == null &&
            mount.read_only == false
          ])
        ),
        false,
      )
      error_message = "Scientific runtime-cache consumers require deployment.scientific_batch.runtime_cache.enabled, and every consumer must use the Terraform-owned writable fs2-scientific-runtime-cache claim at /cache."
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

    # A Workload requests exactly one extended resource, and Kueue's flavor
    # fallback never crosses a resourceGroup, so an eligible set spanning a
    # full-GPU resource and a MIG-slice resource is not a fallback set: the
    # second entry is unreachable and the work silently never bursts. The
    # workloads stage refuses it too, but by then the infrastructure stage has
    # already created pools, so it is refused here first.
    precondition {
      condition = try(alltrue(concat(
        [
          for model_id, pool_ids in local.root_model_eligible_pool_ids :
          length(pool_ids) == length(distinct(pool_ids)) &&
          length(setsubtract(toset(pool_ids), toset(local.root_scheduling_pool_ids))) == 0 &&
          length(distinct([
            for pool_id in pool_ids : local.root_pool_resource_names[pool_id]
          ])) <= 1
        ],
        [
          # A declaration exists to state a qualification, so an empty one
          # states nothing; a routed model with no eligible pool would render
          # a lane that can never admit.
          for model_id, pool_ids in var.deployment.scheduling.model_eligible_pool_ids :
          length(pool_ids) > 0
        ],
        [
          for model_id in local.root_routed_model_ids :
          length(try(local.root_model_eligible_pool_ids[model_id], [])) > 0
        ],
        [length(local.root_declared_placement_collisions) == 0],
      )), false)
      error_message = "Every eligible accelerator pool set must be duplicate-free, name only selected pools, and advertise a single extended resource name, because Kueue cannot fall back across resource names; a declared set must be non-empty and every routed model must have one. A declaration may not overwrite a model that already has an authoritative serving placement: ${join(", ", local.root_declared_placement_collisions)}."
    }

    # With core admission on, an auxiliary prefix must not shadow cpu or memory
    # either, because Kueue matches prefixes literally.
    precondition {
      condition = try(
        !local.root_core_admission_enabled || alltrue([
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
          local.root_scheduling_queue_pool_order_is_warm_first[queue_name] &&
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
      error_message = "Root scheduling pool orders must be exact, duplicate-free, and warm-first: an explicit order may reorder equally stable pools but may not put preemptible or scale-from-zero capacity ahead of a warmer tier. Pool quota and AdmissionCheck pool keys must be selected pools; quota/limits must be whole and nonnegative, limits require a cohort, lending cannot exceed nominal, and summed floors cannot exceed effective accelerator capacity."
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
            (
              !contains(keys(local.root_scheduling_cluster_queues), queue.cluster_queue) ? true : (
                # The resolved preference, so an unset one inherits its own
                # queue's order and only a real disagreement fails.
                local.root_service_class_pool_preference[service_class] ==
                local.root_scheduling_queue_pool_order[queue.cluster_queue] &&
                (!contains(local.root_high_priority_service_classes, service_class) || (
                  contains(
                    ["LowerPriority", "Any"],
                    local.root_scheduling_cluster_queues[queue.cluster_queue].preemption.reclaim_within_cohort,
                    ) && contains(
                    ["LowerPriority", "LowerOrNewerEqualPriority"],
                    local.root_scheduling_cluster_queues[queue.cluster_queue].preemption.within_cluster_queue,
                  )
                ))
              )
            )
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
          local.root_service_class_pool_preference[service_class] ==
          local.root_scheduling_queue_pool_order[
            local.root_scheduling_local_queues[
              coalesce(policy.default_local_queue, local.root_default_local_queue_name)
            ].cluster_queue
          ] &&
          (!contains(local.root_high_priority_service_classes, service_class) || (
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
          local.root_core_admission_enabled
        )
      )
      error_message = "deployment.scheduling.academic_raw_data_stages requires enabled academic execution, an enabled reference-data plane, and scheduling.budget_core_resources so Kueue counts the cpu and memory a raw stage requests. Leave it false to run licensed models from enriched inputs only."
    }

    # Mirror of the workloads implication, before any stage mutates anything.
    precondition {
      condition = (
        local.root_core_admission_enabled ||
        (
          !var.deployment.storage.reference_data.enabled &&
          length(var.deployment.scheduling.cpu_stage_requests) == 0
        )
      )
      error_message = "Enabling the reference-data plane or declaring a CPU stage request means core-requesting work must actually be admitted, but Kueue drops cpu and memory before admission unless core admission is on. Set deployment.scheduling.budget_core_resources and declare measured per-node capacity for every selected accelerator pool."
    }

    # Measured capacity is a claim about a specific node group at a specific
    # time. Without an origin it cannot be checked, and a stale or invented
    # pair of integers would size real quota, so the evidence is validated
    # rather than merely carried.
    precondition {
      condition = alltrue([
        for pool_id, measured in local.root_accelerator_node_sizes : (
          measured.evidence.pool_id == pool_id &&
          length(trimspace(measured.evidence.source)) >= 1 &&
          length(measured.evidence.source) <= 253 &&
          can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]+)?Z$", measured.evidence.captured_at)) &&
          can(regex("^[0-9a-f]{64}$", measured.evidence.payload_sha256)) &&
          (
            !startswith(measured.evidence.source, "fixture:utf8:") || (
              trimprefix(measured.evidence.source, "fixture:utf8:") == pool_id &&
              sha256(trimprefix(measured.evidence.source, "fixture:utf8:")) == measured.evidence.payload_sha256
            )
          ) &&
          (
            try(measured.evidence.node_group_id, null) == null ||
            can(regex("^mk8snodegroup-[a-z0-9]+$", measured.evidence.node_group_id))
          )
        )
      ])
      error_message = "Measured accelerator capacity must name the pool it was read from, a non-empty source, an RFC3339 UTC capture time, a 64-character lowercase SHA-256 of the payload it came from, and a real node-group ID when one is given. A fixture:utf8:<pool-id> source is explicitly non-live and its digest must be the SHA-256 of that exact UTF-8 pool ID. A pair of integers with no truthful origin is not a measurement."
    }

    # Pool-coupled core admission, mirrored from the scheduling module so an
    # impossible budget is refused before infrastructure mutates anything.
    #
    # There is no aggregate to validate: each pool's budget is its measured
    # per-node capacity times its maximum node count, and cpu and memory ride
    # in that pool's own resourceGroup. What has to hold is that every
    # selected pool states a measurement, that no measurement exceeds the
    # machine's nominal size, and that the deployment advertises exactly one
    # accelerator resource key, which is this release's stated limit: a
    # resource belongs to exactly one resourceGroup, so a second accelerator
    # resource cannot be coupled to cpu and memory at all.
    precondition {
      condition = try(
        !local.root_core_admission_enabled ? true : (
          length(local.root_accelerator_pools_missing_measured_core) == 0 &&
          length(local.root_accelerator_resource_names) == 1 &&
          alltrue([
            for pool_id, measured in local.root_accelerator_node_sizes :
            floor(measured.cpu_millicores) == measured.cpu_millicores &&
            measured.cpu_millicores >= 1 &&
            floor(measured.memory_mib) == measured.memory_mib &&
            measured.memory_mib >= 1
          ]) &&
          alltrue([
            for pool_id, nominal in local.root_accelerator_nominal_node_sizes :
            !contains(keys(local.root_accelerator_node_sizes), pool_id) ? true : (
              local.root_accelerator_node_sizes[pool_id].cpu_millicores <= nominal.cpu_millicores &&
              local.root_accelerator_node_sizes[pool_id].memory_mib <= nominal.memory_mib
            )
          ])
        ),
        false,
      )
      error_message = "Core admission in this release supports exactly one accelerator resource name per deployment, and the selected pools advertise ${length(local.root_accelerator_resource_names)} (${join(", ", local.root_accelerator_resource_names)}). Pools may differ in GPU class, node size and capacity, but they must advertise the same resource key, because cpu and memory share the accelerator resourceGroup and a resource belongs to exactly one group. It also requires a measured per-node cpu and memory capacity for every selected accelerator pool (missing: ${join(", ", local.root_accelerator_pools_missing_measured_core)}), each whole, positive and at most the machine's nominal size. Leave scheduling.budget_core_resources off to run a mixed-resource deployment without core quota."
    }

    # One Pod must fit one node and the quota of the queue that admits it.
    precondition {
      condition = try(
        alltrue([
          for class_name, request in local.root_cpu_stage_requests :
          floor(request.cpu_millicores) == request.cpu_millicores && request.cpu_millicores >= 1 &&
          floor(request.memory_mib) == request.memory_mib && request.memory_mib >= 1 &&
          # Checked against the facts this facade actually has for the class,
          # not against a hard-coded class name. reference-data is derived
          # from the reference plane; any other class, general-cpu included,
          # becomes checkable here the moment its producer's facts are
          # supplied through scheduling.cpu_stage_class_facts. A request for a
          # class with no facts is refused rather than validated against
          # capacity nobody declared.
          contains(keys(local.root_cpu_stage_class_facts), class_name) &&
          request.cpu_millicores <= local.root_cpu_stage_class_facts[class_name].schedulable_capacity.cpu_millicores &&
          request.memory_mib <= local.root_cpu_stage_class_facts[class_name].schedulable_capacity.memory_mib &&
          local.root_cpu_stage_class_facts[class_name].queue.nominal_cpu_millicores >= request.cpu_millicores &&
          local.root_cpu_stage_class_facts[class_name].queue.nominal_memory_mib >= request.memory_mib &&
          # The contract carries integer MiB, so a Ki quota that is not a
          # whole MiB is refused rather than silently truncated.
          floor(local.root_cpu_stage_class_facts[class_name].queue.nominal_memory_mib) ==
          local.root_cpu_stage_class_facts[class_name].queue.nominal_memory_mib &&
          floor(local.root_cpu_stage_class_facts[class_name].queue.nominal_cpu_millicores) ==
          local.root_cpu_stage_class_facts[class_name].queue.nominal_cpu_millicores
        ]),
        false,
      )
      error_message = "Every CPU stage request must name a class this deployment has facts for, must be whole and positive, must fit inside that class's per-node schedulable capacity, and must fit inside the nominal cpu/memory quota of the ClusterQueue that admits it. The raw AlphaFold 3 data stage needs 16000m and 65536Mi, so the reference-data pool must advertise at least that per node and its ClusterQueue must budget at least that much; Kubernetes allocatable is lower than a machine preset's nominal size, so declare measured schedulable capacity rather than the preset name. A class another owner renders, such as general-cpu, needs its facts in deployment.scheduling.cpu_stage_class_facts before a request for it can be checked here."
    }

    # Kueue defaults every unspecified resource to weight 1, so a partial map
    # leaves the terms it omits at their raw magnitudes and the control would
    # claim a normalization it has not performed. Either the policy is empty,
    # which changes nothing, or it covers every budgeted resource exactly.
    # Zero is allowed and means the resource is ignored in the ordering.
    precondition {
      condition = (
        length(var.deployment.scheduling.fair_share_resource_weights) == 0 ||
        (
          toset(keys(var.deployment.scheduling.fair_share_resource_weights)) ==
          toset(local.root_budgeted_resource_names) &&
          alltrue([
            for weight in values(var.deployment.scheduling.fair_share_resource_weights) :
            weight >= 0
          ]) &&
          # An all-zero policy orders nothing at all, which is not a policy.
          anytrue([
            for weight in values(var.deployment.scheduling.fair_share_resource_weights) :
            weight > 0
          ])
        )
      )
      error_message = "A fair-share resource weight policy must be empty or complete: Kueue defaults every unspecified resource to weight 1, so a partial map leaves the omitted terms at their raw magnitudes and memory bytes keep dominating. Name exactly the resources this deployment budgets (${join(", ", local.root_budgeted_resource_names)}), with nonnegative weights, zero to ignore a resource, and at least one positive."
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
      condition = length(setintersection(
        toset(local.general_cpu_pool_ids),
        toset(keys(var.deployment.accelerator_pools)),
      )) == 0
      error_message = "A general CPU pool ID must not collide with an accelerator pool ID; each pool has exactly one owner, node group and flavor."
    }

    precondition {
      condition = (
        !local.general_cpu_enabled ||
        !var.deployment.storage.reference_data.enabled ||
        !contains(local.general_cpu_pool_ids, "reference-data")
      )
      error_message = "A general CPU pool cannot be named reference-data; general aggregation capacity is never labelled or reported as the dedicated reference-data plane."
    }

    # A general CPU lane budgets cpu and memory and nothing else. While core
    # admission is off, Kueue's excludeResourcePrefixes drop both before
    # admission, so the lane's quota is not a smaller quota but no quota at
    # all, and the reference-data lane's is inert too. Refused here, before
    # the infrastructure stage creates a node group.
    precondition {
      condition     = !local.general_cpu_enabled || local.root_core_admission_enabled
      error_message = "A general CPU pool budgets cpu and memory, which Kueue excludes from admission unless deployment.scheduling.budget_core_resources is set. Turn it on and declare measured per-node capacity for every selected accelerator pool, or remove the CPU pool."
    }

    # Turning core admission on makes cpu and memory part of every queue's
    # arithmetic. A queue's core share follows its accelerator share of the
    # same pool, so a queue with an accelerator floor has a core floor too;
    # a zero-floor queue has neither and needs the Cohort to borrow both.
    precondition {
      condition = (
        !local.root_core_admission_enabled ||
        var.deployment.scheduling.cohort.enabled ||
        alltrue([
          for queue_name, queue in local.root_scheduling_cluster_queues :
          sum(concat([0], [
            for quota in values(queue.pool_quotas) : quota.nominal_quota
          ])) >= 1
        ])
      )
      error_message = "Core-resource admission is on and the Cohort is disabled, so every accelerator ClusterQueue needs an accelerator floor of its own; with no floor and nothing to borrow from it can reserve neither accelerators nor the cpu and memory that sit beside them, and it would stop admitting."
    }

    precondition {
      condition = (
        !local.general_cpu_enabled ||
        length(local.general_cpu_namespace) > 0
      )
      error_message = "The general CPU lane has capacity but no namespace to admit from. Enable the academic tenant or set scheduling.general_cpu.namespace so the pool is reachable."
    }

    # v1 freezes one class onto one pool. Kueue reports the flavor it admitted
    # through, so a lane spanning several pools could not tell a consumer which
    # node group actually ran a stage.
    precondition {
      condition     = length(var.deployment.cpu_pools) <= 1
      error_message = "The v1 general CPU lane accepts exactly one pool. Declaring several would require namespace- and pool-qualified class identities across Terraform and the controller, which this contract version does not have."
    }

    # Checked before any node group exists, so an undersized general pool is a
    # plan-time answer rather than a BindCraft Job that never schedules.
    precondition {
      condition     = length(local.general_cpu_fit_violations) == 0
      error_message = "A workload bound to the general-cpu class does not fit any declared general CPU pool. Raise deployment.cpu_pools.<pool>.preset and its measured schedulable_capacity. ${join("; ", local.general_cpu_fit_violations)}."
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
