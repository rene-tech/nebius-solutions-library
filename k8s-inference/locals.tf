locals {
  approved_target_contract = jsondecode(file("${path.module}/catalog/profiles/approved-targets.json"))
  capacity_contract        = jsondecode(file("${path.module}/catalog/profiles/capacity-profiles.json"))
  accelerator_contract     = jsondecode(file("${path.module}/catalog/profiles/accelerator-pools.json"))
  pool_profile_contract    = jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json"))
  model_profile_contract   = jsondecode(file("${path.module}/catalog/profiles/model-profiles.json"))

  # Scientific execution is generated with the source catalog and checked in as
  # one reviewed object. A customer enabling the feature should not have to copy
  # that generated object into terraform.tfvars. The optional input remains an
  # advanced override, but every consumer below receives this one effective
  # value without selecting or rebuilding any of its fields.
  committed_scientific_execution_map = jsondecode(file(
    "${path.module}/catalog/runtime/contracts/scientific-execution-map.json"
  ))
  scientific_workload_profile_contract = jsondecode(file(
    "${path.module}/catalog/runtime/contracts/scientific-workload-profiles.json"
  ))
  scientific_workload_profiles_by_model_id = {
    for profile in local.scientific_workload_profile_contract.profiles :
    profile.model_id => profile
  }
  scientific_execution_map = (
    var.deployment.scientific_batch.execution_map == null ?
    local.committed_scientific_execution_map :
    var.deployment.scientific_batch.execution_map
  )
  scientific_execution_map_source = (
    var.deployment.scientific_batch.execution_map == null ?
    "catalog/runtime/contracts/scientific-execution-map.json" :
    "deployment.scientific_batch.execution_map"
  )
  # Helm's scientific-execution-map template hashes the bytes emitted by
  # `toJson`. For this JSON-compatible value those bytes are Terraform's
  # `jsonencode` bytes too; the Helm test renders and compares them directly.
  scientific_execution_map_sha256 = sha256(jsonencode(local.scientific_execution_map))
  scientific_runtime_cache_mounts = flatten([
    for model in try(local.scientific_execution_map.models, []) : [
      for stage in try(model.stages, []) : [
        for mount in try(stage.mounts, []) : mount
        if try(mount.kind, "") == "runtime-cache"
      ]
    ]
  ])

  capacity_profile    = var.deployment.profiles.capacity
  accelerator_profile = coalesce(var.deployment.profiles.accelerators, local.capacity_profile)
  model_profile       = var.deployment.profiles.models

  catalog_target                 = try(local.approved_target_contract.targets[var.deployment.target.project_id], null)
  selected_capacity              = local.capacity_contract.capacity_profiles[local.capacity_profile]
  selected_pool_profile          = local.pool_profile_contract.profiles[local.accelerator_profile]
  selected_model_profile         = local.model_profile_contract.profiles[local.model_profile]
  using_custom_accelerator_pools = length(var.deployment.accelerator_pools) > 0

  target_override_requested = anytrue([
    var.deployment.target.project_name != null,
    var.deployment.target.network.network_name != null,
    var.deployment.target.network.subnet_name != null,
    var.deployment.target.network.private_subnet_cidr != null,
    var.deployment.target.system_update_strategy != null,
  ])

  # The extended resource each selected pool advertises. A profile pool takes
  # it from its accelerator class in the v2 contract.
  root_accelerator_resource_names = sort(distinct(
    local.using_custom_accelerator_pools ? [
      for pool in values(var.deployment.accelerator_pools) : pool.resource_name
      ] : [
      for pool_id in keys(local.selected_pool_profile.pools) :
      local.accelerator_contract.accelerator_classes[
        local.accelerator_contract.pool_templates[pool_id].accelerator_class
      ].resource_api.resource_name
    ]
  ))
  # The same resource name, keyed by pool, so an eligible pool set can be
  # checked for the one property Kueue cares about: a Workload requests
  # exactly one extended resource and flavor fallback never crosses a
  # resourceGroup, so a set spanning two resource names is not a fallback set.
  root_pool_resource_names = local.using_custom_accelerator_pools ? {
    for pool_id, pool in var.deployment.accelerator_pools : pool_id => pool.resource_name
    } : {
    for pool_id in keys(local.selected_pool_profile.pools) : pool_id => (
      local.accelerator_contract.accelerator_classes[
        local.accelerator_contract.pool_templates[pool_id].accelerator_class
      ].resource_api.resource_name
    )
  }
  # Every RDMA resource any ModelExpress model may request, from its default
  # transport and from each per-pool override.
  kueue_auxiliary_resource_prefixes = sort(distinct(compact(flatten([
    for model in values(var.deployment.acceleration.model_express.models) : concat(
      [model.transport.rdma_resource_name],
      [for transport in values(model.pool_transports) : transport.rdma_resource_name],
    )
  ]))))

  resolved_target_binding = {
    project_id   = var.deployment.target.project_id
    project_name = try(coalesce(var.deployment.target.project_name, try(local.catalog_target.project_name, null)), null)
    region       = var.deployment.target.region
    network_name = try(coalesce(var.deployment.target.network.network_name, try(local.catalog_target.network_name, null)), null)
    subnet_name  = try(coalesce(var.deployment.target.network.subnet_name, try(local.catalog_target.subnet_name, null)), null)
    private_subnet_cidr = try(coalesce(
      var.deployment.target.network.private_subnet_cidr,
      try(local.catalog_target.private_subnet_cidr, null),
    ), null)
    system_update_strategy = try(coalesce(
      var.deployment.target.system_update_strategy,
      try(local.catalog_target.system_update_strategy, null),
    ), null)
  }

  run_id = "r${substr(sha256(jsonencode({
    name       = var.deployment.name
    project_id = var.deployment.target.project_id
    region     = var.deployment.target.region
  })), 0, 10)}"

  reference_data_bucket_name = coalesce(
    var.deployment.storage.reference_data.object_storage.bucket_name,
    "${var.deployment.name}-${local.run_id}-reference-data",
  )

  scientific_artifacts_bucket_name = coalesce(
    var.deployment.storage.scientific_artifacts.object_storage.bucket_name,
    "${var.deployment.name}-${local.run_id}-scientific-artifacts",
  )

  # General CPU pools. Capacity mode is exactly one of fixed or autoscaling, so
  # the effective bounds are unambiguous and the lane's nominal quota is derived
  # from the maximum node count an operator actually authorized.
  general_cpu_pool_bounds = {
    for pool_id, pool in var.deployment.cpu_pools : pool_id => {
      min_nodes = pool.fixed_nodes != null ? pool.fixed_nodes : pool.autoscaling.min_nodes
      max_nodes = pool.fixed_nodes != null ? pool.fixed_nodes : pool.autoscaling.max_nodes
      elastic   = pool.fixed_nodes == null
    }
  }
  general_cpu_pool_ids = sort(keys(var.deployment.cpu_pools))
  general_cpu_enabled  = length(var.deployment.cpu_pools) > 0
  # Quotas are measured capacity times authorized nodes, never a preset guess.
  general_cpu_lane_capacity = {
    cpu_millicores = sum(concat([0], [
      for pool_id, pool in var.deployment.cpu_pools :
      pool.schedulable_capacity.cpu_millicores * local.general_cpu_pool_bounds[pool_id].max_nodes
    ]))
    memory_mib = sum(concat([0], [
      for pool_id, pool in var.deployment.cpu_pools :
      pool.schedulable_capacity.memory_mib * local.general_cpu_pool_bounds[pool_id].max_nodes
    ]))
    ephemeral_storage_mib = sum(concat([0], [
      for pool_id, pool in var.deployment.cpu_pools :
      pool.schedulable_capacity.ephemeral_storage_mib * local.general_cpu_pool_bounds[pool_id].max_nodes
    ]))
  }
  # The largest single node in the lane. A pod cannot be split across nodes, so
  # this is what a consumer must compare a per-pod request against.
  general_cpu_largest_node = {
    cpu_millicores = max(0, [
      for pool in values(var.deployment.cpu_pools) : pool.schedulable_capacity.cpu_millicores
    ]...)
    memory_mib = max(0, [
      for pool in values(var.deployment.cpu_pools) : pool.schedulable_capacity.memory_mib
    ]...)
    ephemeral_storage_mib = max(0, [
      for pool in values(var.deployment.cpu_pools) : pool.schedulable_capacity.ephemeral_storage_mib
    ]...)
  }
  # Scheduling is authoritative about what the general lane must be able to run.
  # The bound workloads and their capacity live in the checked-in CPU stage
  # class contract, so the fit is checked here, at the root, before any node
  # group is created rather than when a BindCraft aggregation Job first fails
  # to schedule.
  cpu_class_contract = jsondecode(file("${path.module}/scheduling/cpu-class-contract.json"))
  general_cpu_bound_workloads = [
    for workload in local.cpu_class_contract.classes["general-cpu"].bound_workloads : {
      model_id       = workload.model_id
      stage          = workload.stage
      cpu_millicores = tonumber(workload.capacity.cpu) * 1000
      memory_mib     = tonumber(trimsuffix(workload.capacity.memory, "Gi")) * 1024
    }
  ]
  # One stage Pod runs on one node, so every bound workload must fit the largest
  # node the lane offers, not the lane total.
  general_cpu_fit_violations = local.general_cpu_enabled ? [
    for workload in local.general_cpu_bound_workloads : format(
      "%s %s needs %d millicores and %d MiB but the largest general CPU node schedules only %d millicores and %d MiB",
      workload.model_id,
      workload.stage,
      workload.cpu_millicores,
      workload.memory_mib,
      local.general_cpu_largest_node.cpu_millicores,
      local.general_cpu_largest_node.memory_mib,
    )
    if workload.cpu_millicores > local.general_cpu_largest_node.cpu_millicores ||
    workload.memory_mib > local.general_cpu_largest_node.memory_mib
  ] : []

  general_cpu_lane = {
    enabled             = local.general_cpu_enabled
    cluster_queue       = var.deployment.scheduling.general_cpu.cluster_queue
    local_queue         = var.deployment.scheduling.general_cpu.local_queue
    resource_flavor     = var.deployment.scheduling.general_cpu.cluster_queue
    queueing_strategy   = var.deployment.scheduling.general_cpu.queueing_strategy
    fair_sharing_weight = var.deployment.scheduling.general_cpu.fair_sharing_weight
  }
  # Exactly one execution namespace, and always a namespace some owner actually
  # creates. It defaults to the academic tenant when that tenant exists, because
  # a licensed BindCraft stage must run beside the claim it mounts; otherwise it
  # falls back to fs2-models, which the platform always provisions. An operator
  # naming another namespace is responsible for creating it.
  general_cpu_default_namespace = var.academic_assets.enabled ? var.academic_assets.namespace : "fs2-models"
  general_cpu_namespace = coalesce(
    var.deployment.scheduling.general_cpu.namespace,
    local.general_cpu_default_namespace,
  )

  selected_model_ids = sort(tolist(
    var.deployment.models.selection == "profile" ?
    toset(local.selected_model_profile.canonical_routes) :
    var.deployment.models.enabled
  ))
  selected_runtime_model_contracts = {
    for model_id in local.selected_model_ids :
    model_id => jsondecode(file("${path.module}/catalog/runtime/models/${model_id}.json"))
  }
  effective_model_images = {
    for model_id, model in local.selected_runtime_model_contracts : model_id => try(
      var.deployment.models.image_overrides[model_id],
      model.runtime.image.reference,
    )
  }
  selected_model_required_secrets = toset(distinct(flatten([
    for model_id in local.selected_model_ids : try(
      local.model_profile_contract.model_artifacts[model_id].required_secrets,
      [],
    )
  ])))
  selected_image_source_hosts = sort(distinct(concat(
    [
      split("/", var.deployment.applications.control_plane.repository)[0],
      split("/", var.deployment.applications.admin_console.repository)[0],
    ],
    [for image in values(local.effective_model_images) : split("/", image)[0]],
    var.deployment.storage.reference_data.status.enabled ? [split("/", var.deployment.storage.reference_data.status.image)[0]] : [],
    var.deployment.storage.reference_data.pipeline.enabled ? [split("/", var.deployment.storage.reference_data.pipeline.image)[0]] : [],
  )))
  reference_data_pipeline_cpu_millicores = endswith(var.deployment.storage.reference_data.pipeline.cpu, "m") ? tonumber(trimsuffix(var.deployment.storage.reference_data.pipeline.cpu, "m")) : tonumber(var.deployment.storage.reference_data.pipeline.cpu) * 1000
  reference_data_pipeline_memory_parts   = regex("^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$", var.deployment.storage.reference_data.pipeline.memory)
  reference_data_pipeline_memory_mib     = tonumber(local.reference_data_pipeline_memory_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.reference_data_pipeline_memory_parts[1])
  reference_data_pipeline_ephemeral_parts = regex(
    "^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$",
    var.deployment.storage.reference_data.pipeline.ephemeral_storage,
  )
  reference_data_pipeline_ephemeral_mib = tonumber(local.reference_data_pipeline_ephemeral_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.reference_data_pipeline_ephemeral_parts[1])
  reference_data_queue_cpu_millicores   = endswith(var.deployment.storage.reference_data.queue.nominal_cpu, "m") ? tonumber(trimsuffix(var.deployment.storage.reference_data.queue.nominal_cpu, "m")) : tonumber(var.deployment.storage.reference_data.queue.nominal_cpu) * 1000
  reference_data_queue_memory_parts     = regex("^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$", var.deployment.storage.reference_data.queue.nominal_memory)
  reference_data_queue_memory_mib       = tonumber(local.reference_data_queue_memory_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.reference_data_queue_memory_parts[1])
  reference_data_status_request = {
    cpu_millicores        = 50
    memory_mib            = 64
    ephemeral_storage_mib = 64
  }
  reference_data_required_capacity = {
    cpu_millicores = (
      (var.deployment.storage.reference_data.pipeline.enabled ? local.reference_data_pipeline_cpu_millicores : 0) +
      (var.deployment.storage.reference_data.status.enabled ? local.reference_data_status_request.cpu_millicores * var.deployment.storage.reference_data.status.replicas : 0)
    )
    memory_mib = (
      (var.deployment.storage.reference_data.pipeline.enabled ? local.reference_data_pipeline_memory_mib : 0) +
      (var.deployment.storage.reference_data.status.enabled ? local.reference_data_status_request.memory_mib * var.deployment.storage.reference_data.status.replicas : 0)
    )
    ephemeral_storage_mib = (
      (var.deployment.storage.reference_data.pipeline.enabled ? local.reference_data_pipeline_ephemeral_mib : 0) +
      (var.deployment.storage.reference_data.status.enabled ? local.reference_data_status_request.ephemeral_storage_mib * var.deployment.storage.reference_data.status.replicas : 0)
    )
  }
  reference_data_total_schedulable_capacity = {
    for resource, capacity in var.deployment.storage.reference_data.cpu_pool.schedulable_capacity :
    resource => capacity * var.deployment.storage.reference_data.cpu_pool.node_count
  }

  # Nebius Managed Kubernetes builds the cluster-autoscaler template for a
  # zero-node pool from its network boot disk. Host-local NVMe is visible only
  # after a real node joins. The catalog's scheduling request is checked
  # against every selected Deployment manifest by the deployment-contract test.
  selected_model_effective_ephemeral_request_gib = {
    for model_id in local.selected_model_ids :
    model_id => local.model_profile_contract.model_autoscaling_targets[model_id].ephemeral_storage_request_gib
  }

  managed_autoscaler_boot_disk_allocatable_ratio = 0.80
  managed_autoscaler_ephemeral_headroom_gib      = 32

  accelerator_pool_capacity_overrides = {
    for pool_id, bounds in var.deployment.accelerator_pool_capacity : pool_id => {
      min_nodes = bounds.min_nodes
      max_nodes = bounds.max_nodes
    }
  }
  effective_pool_capacities = local.using_custom_accelerator_pools ? {
    for pool_id, pool in var.deployment.accelerator_pools : pool_id => {
      min_nodes = pool.min_nodes
      max_nodes = pool.max_nodes
    }
    } : {
    for pool_id, bounds in local.selected_pool_profile.pools : pool_id => {
      min_nodes = try(var.deployment.accelerator_pool_capacity[pool_id].min_nodes, bounds.floor_nodes.zero)
      max_nodes = try(var.deployment.accelerator_pool_capacity[pool_id].max_nodes, bounds.max_nodes)
    }
  }
  effective_pool_facts = local.using_custom_accelerator_pools ? {
    for pool_id, pool in var.deployment.accelerator_pools : pool_id => {
      accelerator_class  = pool.accelerator_class
      gpus_per_node      = pool.gpus_per_node
      host_architectures = [pool.host_architecture]
      boot_disk_gib      = pool.boot_disk.size_gib
      scale_from_zero    = pool.min_nodes == 0 && pool.topology.mode != "nvlink_rack"
    }
    } : {
    for pool_id in keys(local.selected_pool_profile.pools) : pool_id => {
      accelerator_class  = local.accelerator_contract.pool_templates[pool_id].accelerator_class
      gpus_per_node      = local.accelerator_contract.pool_templates[pool_id].node.gpus_per_node
      host_architectures = local.accelerator_contract.pool_templates[pool_id].node.host_architectures
      boot_disk_gib      = try(local.accelerator_contract.pool_templates[pool_id].node.boot_disk.size_gib, 0)
      scale_from_zero = (
        local.effective_pool_capacities[pool_id].min_nodes == 0 &&
        local.accelerator_contract.pool_templates[pool_id].capacity.scale_from_zero &&
        local.accelerator_contract.pool_templates[pool_id].node.topology != "nvlink_rack"
      )
    }
  }

  # Root scheduling projection. These are the same effective pool, stable
  # queue, and route facts consumed by the workloads module, evaluated before
  # any staged infrastructure mutation can begin.
  # The same capacity-derived order the scheduling module applies, mirrored
  # here so the facade validates the order the workloads stage will render.
  # Alphabetical pool IDs would put "h100-preemptible" ahead of "h100-warm".
  root_scheduling_pool_ids = concat(
    sort([
      for pool_id in keys(local.effective_pool_capacities) : pool_id
      if !local.root_pool_is_preemptible[pool_id] && local.root_pool_min_nodes[pool_id] > 0
    ]),
    sort([
      for pool_id in keys(local.effective_pool_capacities) : pool_id
      if !local.root_pool_is_preemptible[pool_id] && local.root_pool_min_nodes[pool_id] == 0
    ]),
    sort([
      for pool_id in keys(local.effective_pool_capacities) : pool_id
      if local.root_pool_is_preemptible[pool_id] && local.root_pool_min_nodes[pool_id] > 0
    ]),
    sort([
      for pool_id in keys(local.effective_pool_capacities) : pool_id
      if local.root_pool_is_preemptible[pool_id] && local.root_pool_min_nodes[pool_id] == 0
    ]),
  )
  root_pool_is_preemptible = {
    for pool_id in keys(local.effective_pool_capacities) : pool_id => (
      local.using_custom_accelerator_pools
      ? var.deployment.accelerator_pools[pool_id].capacity_type == "preemptible"
      : try(
        local.accelerator_contract.pool_templates[pool_id].capacity.default_mode,
        "preemptible",
      ) == "preemptible"
    )
  }
  root_pool_min_nodes = {
    for pool_id in keys(local.effective_pool_capacities) : pool_id =>
    coalesce(try(local.effective_pool_capacities[pool_id].min_nodes, 0), 0)
  }
  root_pool_stability_tier = {
    for pool_id in local.root_scheduling_pool_ids : pool_id => (
      !local.root_pool_is_preemptible[pool_id] && local.root_pool_min_nodes[pool_id] > 0 ? 0 :
      !local.root_pool_is_preemptible[pool_id] ? 1 :
      local.root_pool_min_nodes[pool_id] > 0 ? 2 : 3
    )
  }
  root_capacity_ordered_pool_ids = concat(
    sort([for pool_id in local.root_scheduling_pool_ids : pool_id if local.root_pool_stability_tier[pool_id] == 0]),
    sort([for pool_id in local.root_scheduling_pool_ids : pool_id if local.root_pool_stability_tier[pool_id] == 1]),
    sort([for pool_id in local.root_scheduling_pool_ids : pool_id if local.root_pool_stability_tier[pool_id] == 2]),
    sort([for pool_id in local.root_scheduling_pool_ids : pool_id if local.root_pool_stability_tier[pool_id] == 3]),
  )
  root_scheduling_pool_capacity = {
    for pool_id in local.root_scheduling_pool_ids :
    pool_id => coalesce(try(local.effective_pool_facts[pool_id].gpus_per_node, null), 0) * coalesce(try(local.effective_pool_capacities[pool_id].max_nodes, null), 0)
  }
  # Licensed academic work runs in the claim namespace, so the stable
  # ClusterQueue must admit that namespace too. This mirrors the workloads
  # stage exactly, one stage earlier.
  root_academic_execution_enabled = var.academic_assets.enabled && var.academic_assets.execution.enabled
  root_academic_model_ids = local.root_academic_execution_enabled ? sort(distinct([
    for asset in values(var.academic_assets.assets) : asset.model_id
  ])) : []
  root_default_cluster_queue_name  = local.using_custom_accelerator_pools ? "inference-accelerators" : local.selected_pool_profile.queue.cluster_queue_name
  root_default_local_queue_name    = local.using_custom_accelerator_pools ? "inference-models" : local.selected_pool_profile.queue.local_queue_name
  root_academic_cluster_queue_name = var.academic_assets.execution.cluster_queue
  root_academic_local_queue_name   = var.academic_assets.execution.local_queue
  root_academic_local_queues = local.root_academic_execution_enabled ? {
    (local.root_academic_local_queue_name) = {
      namespace           = var.academic_assets.namespace
      cluster_queue       = local.root_academic_cluster_queue_name
      fair_sharing_weight = 1
      model_ids           = toset(local.root_academic_model_ids)
      tenant_ids          = toset([var.academic_assets.tenant_id])
      service_classes = toset([
        "platform-critical",
        "presentation",
        "interactive",
        "customer-batch",
        "bulk-backfill",
      ])
    }
  } : {}
  root_default_cluster_queue = {
    namespace              = "fs2-models"
    queueing_strategy      = local.using_custom_accelerator_pools ? "BestEffortFIFO" : local.selected_pool_profile.queue.queueing_strategy
    fair_sharing_weight    = 1
    admission_fair_sharing = true
    flavor_order = (
      length(var.deployment.scheduling.default_queue_pool_order) == 0
      ? local.root_capacity_ordered_pool_ids
      : var.deployment.scheduling.default_queue_pool_order
    )
    flavor_fungibility = {
      when_can_borrow  = "MayStopSearch"
      when_can_preempt = "TryNextFlavor"
      preference       = null
    }
    admission_checks                   = []
    namespaces                         = []
    fair_share_precedence_acknowledged = var.deployment.scheduling.fair_share_precedence_acknowledged
    # A zero core floor: the stable queue borrows core capacity from the Cohort
    # rather than reserving it, matching the scheduling module's default.
    pool_quotas = {
      for pool_id, capacity in local.root_scheduling_pool_capacity : pool_id => {
        nominal_quota   = length(var.deployment.scheduling.cluster_queues) == 0 ? capacity : 0
        borrowing_limit = null
        lending_limit   = null
      }
    }
    preemption = {
      reclaim_within_cohort = "LowerPriority"
      within_cluster_queue  = "LowerPriority"
    }
  }
  root_default_local_queue = {
    namespace           = "fs2-models"
    cluster_queue       = local.root_default_cluster_queue_name
    fair_sharing_weight = 1
    model_ids           = toset([])
    tenant_ids          = toset([])
    service_classes     = toset([])
  }
  root_scheduling_cluster_queues = merge(
    { (local.root_default_cluster_queue_name) = local.root_default_cluster_queue },
    var.deployment.scheduling.cluster_queues,
  )
  root_academic_lane_queue_collisions = sort([
    for queue_name in concat(
      keys(local.root_academic_local_queues),
      keys(local.root_academic_cpu_local_queues),
    ) : queue_name
    if contains(keys(var.deployment.scheduling.local_queues), queue_name)
  ])
  root_scheduling_local_queues = merge(
    { (local.root_default_local_queue_name) = local.root_default_local_queue },
    var.deployment.scheduling.local_queues,
    local.root_academic_local_queues,
    local.root_academic_cpu_local_queues,
  )
  root_referenceable_cluster_queues = sort(distinct(concat(
    keys(local.root_scheduling_cluster_queues),
    local.root_academic_cpu_lane_enabled ? [local.root_reference_cluster_queue_name] : [],
  )))
  root_scheduling_cluster_queue_namespaces = merge({
    for queue_name, queue in local.root_scheduling_cluster_queues : queue_name => sort(distinct(concat(
      [queue.namespace],
      queue.namespaces,
      try(local.root_required_namespaces[queue_name], []),
    )))
    },
    local.root_academic_cpu_lane_enabled ? {
      (local.root_reference_cluster_queue_name) = sort(distinct(concat(
        [var.deployment.storage.reference_data.namespace],
        [var.academic_assets.namespace],
      )))
    } : {},
  )
  # The eligibility the workloads stage will compute, mirrored here so a bad
  # set fails in the facade rather than after the infrastructure stage has
  # already created pools.
  root_derived_model_eligible_pool_ids = {
    for model_id, placement in local.selected_model_placements : model_id => sort([
      for pool_id in placement.compatible_pool_ids : pool_id
      if contains(local.root_scheduling_pool_ids, pool_id)
    ]) if placement != null
  }
  # A declaration covers a model with no serving placement. It may never
  # overwrite one that has a placement, because that placement is the
  # authoritative qualification record. The workloads stage refuses the
  # collision too, but by then infrastructure has already run.
  root_declared_placement_collisions = sort(tolist(setintersection(
    toset(keys(var.deployment.scheduling.model_eligible_pool_ids)),
    toset(keys(local.root_derived_model_eligible_pool_ids)),
  )))
  root_model_eligible_pool_ids = merge(
    local.root_derived_model_eligible_pool_ids,
    var.deployment.scheduling.model_eligible_pool_ids,
  )
  root_routed_model_ids = sort(distinct(flatten([
    for queue in values(local.root_scheduling_local_queues) : tolist(queue.model_ids)
  ])))
  root_scheduling_queue_pool_order = {
    for queue_name, queue in local.root_scheduling_cluster_queues : queue_name => (
      length(queue.flavor_order) == 0 ? local.root_capacity_ordered_pool_ids : queue.flavor_order
    )
  }
  root_scheduling_queue_pool_order_is_warm_first = {
    for queue_name, order in local.root_scheduling_queue_pool_order : queue_name => alltrue([
      for index, pool_id in order : index == 0 ? true : try(
        local.root_pool_stability_tier[order[index - 1]] <= local.root_pool_stability_tier[pool_id],
        false,
      )
    ])
  }
  # An unset pool_preference inherits the search order of the ClusterQueue the
  # class routes to, mirroring the scheduling module exactly. Alphabetical pool
  # ID order would turn one operator decision into six identical settings and
  # would fail validation for any queue order that is not alphabetical.
  root_service_class_pool_preference = {
    for service_class, policy in var.deployment.scheduling.service_classes :
    service_class => (
      length(policy.pool_preference) > 0 ? policy.pool_preference : try(
        local.root_scheduling_queue_pool_order[
          local.root_scheduling_local_queues[
            coalesce(policy.default_local_queue, local.root_default_local_queue_name)
          ].cluster_queue
        ],
        local.root_scheduling_pool_ids,
      )
    )
  }
  # Mirrors modules/kueue-scheduling: which ClusterQueues serve a protected
  # class, and which serve only lower classes while holding a floor that no
  # cross-queue reclaim can reach.
  root_protected_serving_cluster_queues = sort([
    for queue_name in keys(local.root_scheduling_cluster_queues) : queue_name
    if length(setintersection(
      toset(local.root_high_priority_service_classes),
      toset(flatten([
        for lane_name in local.root_serving_lanes[queue_name] :
        tolist(try(local.root_scheduling_local_queues[lane_name].service_classes, []))
      ])),
    )) > 0
  ])
  root_unreclaimable_lower_priority_queues = sort([
    for queue_name, queue in local.root_scheduling_cluster_queues : queue_name
    if !contains(local.root_protected_serving_cluster_queues, queue_name) &&
    length(local.root_serving_lanes[queue_name]) > 0 &&
    sum(concat([0], [for quota in values(queue.pool_quotas) : quota.nominal_quota])) > 0
  ])
  root_scheduling_nominal_by_pool = {
    for pool_id in local.root_scheduling_pool_ids : pool_id => sum([
      for queue in values(local.root_scheduling_cluster_queues) : try(queue.pool_quotas[pool_id].nominal_quota, 0)
    ])
  }
  # Rank-separated, exactly as modules/kueue-scheduling computes them. An exact
  # tenant route deliberately overlaps a wildcard route; only two routes of the
  # same rank are a conflict. jsonencode keeps the key free of control bytes.
  root_exact_lane_binding_keys = flatten([
    for queue in values(local.root_scheduling_local_queues) : flatten([
      for service_class in queue.service_classes : [
        for model_id in queue.model_ids : [
          for tenant_id in sort(tolist(queue.tenant_ids)) :
          jsonencode([service_class, tenant_id, model_id])
        ]
      ]
    ])
  ])
  root_wildcard_lane_binding_keys = flatten([
    for queue in values(local.root_scheduling_local_queues) : flatten([
      for service_class in queue.service_classes : [
        for model_id in queue.model_ids :
        jsonencode([service_class, model_id]) if length(queue.tenant_ids) == 0
      ]
    ])
  ])
  # Only these classes are selectable by a caller; platform-critical is
  # resolver-internal. A namespace-bound model needs a lane for each of them.
  # Mirrors modules/kueue-scheduling exactly. Interactive is protected: a
  # numeric priority outranks bulk only inside one LocalQueue, and under
  # usage-based admission fair sharing not even there, so an interactive lane
  # needs cohort reclaim and in-queue displacement or it waits behind bulk
  # work that is already admitted.
  root_high_priority_service_classes = ["platform-critical", "presentation", "interactive"]
  root_caller_selectable_service_classes = toset([
    "presentation",
    "interactive",
    "customer-batch",
    "bulk-backfill",
  ])
  root_namespace_bound_models = {
    for model_id in local.root_academic_model_ids : model_id => var.academic_assets.namespace
  }
  root_required_namespaces = merge(
    local.root_academic_execution_enabled ? {
      (local.root_academic_cluster_queue_name) = [var.academic_assets.namespace]
    } : {},
    local.root_academic_cpu_lane_enabled ? {
      (local.root_reference_cluster_queue_name) = [var.academic_assets.namespace]
    } : {},
  )

  # The same route-less CPU lane the workloads stage derives, mirrored here so
  # an invalid identity, a collision, or a request that cannot fit is refused
  # before any stage mutates anything.
  root_academic_cpu_lane_enabled = (
    local.root_academic_execution_enabled &&
    var.deployment.storage.reference_data.enabled &&
    var.deployment.scheduling.academic_raw_data_stages
  )
  # Configurable, exactly as the workloads stage reads it, so a non-default
  # queue name behaves identically in both places.
  root_reference_cluster_queue_name = try(
    var.deployment.storage.reference_data.queue.cluster_queue,
    "reference-data-cpu",
  )
  root_academic_cpu_local_queue_name = format(
    "%s-cpu",
    substr(local.root_academic_local_queue_name, 0, 59),
  )
  root_academic_cpu_local_queues = local.root_academic_cpu_lane_enabled ? {
    (local.root_academic_cpu_local_queue_name) = {
      namespace           = var.academic_assets.namespace
      cluster_queue       = local.root_reference_cluster_queue_name
      fair_sharing_weight = 1
      model_ids           = toset([])
      tenant_ids          = toset([])
      service_classes     = toset([])
    }
  } : {}
  # The canonical raw AlphaFold 3 data-stage request, derived exactly as the
  # workloads stage derives it.
  raw_af3_cpu_request = { cpu_millicores = 16000, memory_mib = 65536 }
  # Per field max, not merge: an override may raise the canonical raw
  # AlphaFold 3 request but can never lower it below the measured need.
  root_cpu_stage_requests = {
    for class_name, request in merge(
      local.root_academic_cpu_lane_enabled ? { reference-data = local.raw_af3_cpu_request } : {},
      var.deployment.scheduling.cpu_stage_requests,
      ) : class_name => class_name != "reference-data" ? request : {
      cpu_millicores = max(request.cpu_millicores, local.raw_af3_cpu_request.cpu_millicores)
      memory_mib     = max(request.memory_mib, local.raw_af3_cpu_request.memory_mib)
    }
  }
  root_reference_cpu_capacity = try(
    var.deployment.storage.reference_data.cpu_pool.schedulable_capacity,
    null,
  )
  root_core_admission_enabled = var.deployment.scheduling.budget_core_resources
  # Each pool's core budget: measured per-node capacity times the pool's
  # maximum node count. Derived, never operator-typed, so it cannot drift from
  # the pools the infrastructure stage creates.
  root_core_pool_capacity = !local.root_core_admission_enabled ? {} : {
    for pool_id in local.root_scheduling_pool_ids : pool_id => {
      cpu_millicores = (
        local.root_accelerator_node_sizes[pool_id].cpu_millicores *
        coalesce(try(local.effective_pool_capacities[pool_id].max_nodes, 0), 0)
      )
      memory_mib = (
        local.root_accelerator_node_sizes[pool_id].memory_mib *
        coalesce(try(local.effective_pool_capacities[pool_id].max_nodes, 0), 0)
      )
    }
    if contains(keys(local.root_accelerator_node_sizes), pool_id)
  }
  # The reference ClusterQueue's own quota, converted to the exact units the
  # scheduling contract uses so a stage request can be compared with it.
  root_reference_queue_cpu_millicores = try(
    endswith(var.deployment.storage.reference_data.queue.nominal_cpu, "m")
    ? tonumber(trimsuffix(var.deployment.storage.reference_data.queue.nominal_cpu, "m"))
    : tonumber(var.deployment.storage.reference_data.queue.nominal_cpu) * 1000,
    0,
  )
  # The whole Ki|Mi|Gi|Ti grammar the facade accepts, so root and workloads
  # convert an identical value identically.
  root_reference_memory_unit_mib = {
    Ki = 1 / 1024
    Mi = 1
    Gi = 1024
    Ti = 1048576
  }
  root_reference_queue_memory_mib = try(
    tonumber(substr(
      var.deployment.storage.reference_data.queue.nominal_memory,
      0,
      length(var.deployment.storage.reference_data.queue.nominal_memory) - 2,
      )) * lookup(
      local.root_reference_memory_unit_mib,
      substr(
        var.deployment.storage.reference_data.queue.nominal_memory,
        length(var.deployment.storage.reference_data.queue.nominal_memory) - 2,
        2,
      ),
      0,
    ),
    0,
  )
  # Every CPU stage class this facade has facts for, keyed by class, derived
  # from the plane that actually creates the capacity. There is deliberately
  # no tfvars surface for these facts: a second operator-authored copy of a
  # pool's capacity and its queue quota can drift from the pool that gets
  # created, and the drift would only show up as an unschedulable stage. A
  # class whose producer is not in this configuration therefore has no facts
  # here and a request for it is refused, rather than checked against numbers
  # nothing creates. Nothing is keyed off a hard-coded class name.
  root_cpu_stage_class_facts = merge(
    local.root_academic_cpu_lane_enabled && local.root_reference_cpu_capacity != null ? {
      reference-data = {
        schedulable_capacity = {
          cpu_millicores = local.root_reference_cpu_capacity.cpu_millicores
          memory_mib     = local.root_reference_cpu_capacity.memory_mib
        }
        queue = {
          nominal_cpu_millicores = local.root_reference_queue_cpu_millicores
          nominal_memory_mib     = local.root_reference_queue_memory_mib
        }
      }
    } : {},
    # The general CPU lane's own facts, from the producer that creates its
    # pool rather than from a knob an operator could set independently: the
    # largest node it can schedule on, and the whole lane quota its
    # ClusterQueue holds. A stage bound to general-cpu is checked against
    # these here, one stage before the pool exists.
    local.general_cpu_enabled ? {
      general-cpu = {
        schedulable_capacity = {
          cpu_millicores = local.general_cpu_largest_node.cpu_millicores
          memory_mib     = local.general_cpu_largest_node.memory_mib
        }
        queue = {
          nominal_cpu_millicores = local.general_cpu_lane_capacity.cpu_millicores
          nominal_memory_mib     = local.general_cpu_lane_capacity.memory_mib
        }
      }
    } : {},
  )
  # The cpu and memory the accelerator pools can physically supply, summed
  # over the pools this deployment selects at their maximum node counts. This
  # is the ceiling for the shared label-less core flavor, which only the
  # accelerator Cohort queues draw on. CPU-only pools are deliberately absent:
  # the reference-data and general-CPU lanes are external ClusterQueues with
  # their own flavors and their own quotas, so counting their nodes here would
  # let a GPU queue reserve core capacity that sits on a node its accelerator
  # ResourceFlavor can never select.
  #
  # Null when a custom pool does not state its node size, because a ceiling
  # that silently omits a pool is worse than no ceiling.
  # Measured per-node allocatable, never a preset's nominal size. A preset
  # states what the machine has; Kubernetes schedules what is left after the
  # kubelet, system and DaemonSet reserve, and the difference is large enough
  # that a quota derived from nominal over-admits. Nothing in this repository
  # measures it, so the operator declares it per pool and a pool that has not
  # is missing from this map.
  root_accelerator_node_sizes = merge(
    {
      for pool_id, pool in var.deployment.accelerator_pools : pool_id => pool.schedulable_capacity
      if pool.schedulable_capacity != null
    },
    var.deployment.scheduling.accelerator_schedulable_capacity,
  )
  # The nominal size the catalog states, used only to refuse a measured value
  # that exceeds what the machine physically has.
  # A declaration-only catalog entry may state no node size at all, so a pool
  # without one simply has no nominal bound rather than breaking evaluation.
  root_accelerator_nominal_node_sizes = local.using_custom_accelerator_pools ? {} : {
    for pool_id in keys(local.selected_pool_profile.pools) : pool_id => {
      cpu_millicores = try(local.accelerator_contract.pool_templates[pool_id].node.vcpu_count, 0) * 1000
      memory_mib     = try(local.accelerator_contract.pool_templates[pool_id].node.memory_gib, 0) * 1024
    }
    if try(local.accelerator_contract.pool_templates[pool_id].node.vcpu_count, null) != null &&
    try(local.accelerator_contract.pool_templates[pool_id].node.memory_gib, null) != null
  }
  root_accelerator_pools_missing_measured_core = sort([
    for pool_id in local.root_scheduling_pool_ids : pool_id
    if !contains(keys(local.root_accelerator_node_sizes), pool_id)
  ])

  # Exactly the resources this deployment budgets: every accelerator resource
  # its pools advertise, plus cpu and memory once core admission is on. A
  # weight for anything else would silently do nothing.
  root_budgeted_resource_names = sort(distinct(concat(
    local.root_accelerator_resource_names,
    local.root_core_admission_enabled ? ["cpu", "memory"] : [],
  )))

  root_serving_lanes = {
    for queue_name, queue in local.root_scheduling_cluster_queues : queue_name => sort(distinct(concat(
      [
        for lane_name, lane in local.root_scheduling_local_queues : lane_name
        if lane.cluster_queue == queue_name && length(lane.service_classes) > 0
      ],
      [
        for service_class, policy in var.deployment.scheduling.service_classes :
        coalesce(policy.default_local_queue, local.root_default_local_queue_name)
        if try(
          local.root_scheduling_local_queues[
            coalesce(policy.default_local_queue, local.root_default_local_queue_name)
          ].cluster_queue,
          null,
        ) == queue_name
      ],
    )))
  }
  root_service_priority_class_groups = {
    for priority_name in distinct([
      for policy in values(var.deployment.scheduling.service_classes) : policy.workload_priority_class
      ]) : priority_name => [
      for policy in values(var.deployment.scheduling.service_classes) : policy.priority
      if policy.workload_priority_class == priority_name
    ]
  }
  effective_pool_synthetic_ephemeral_budget_gib = {
    for pool_id, pool in local.effective_pool_facts : pool_id => max(
      0,
      floor(pool.boot_disk_gib * local.managed_autoscaler_boot_disk_allocatable_ratio) - local.managed_autoscaler_ephemeral_headroom_gib,
    )
  }
  catalog_model_placements = {
    for model_id in local.selected_model_ids : model_id => try(
      local.model_profile_contract.workload_placements[
        local.model_profile_contract.model_autoscaling_targets[model_id].deployment
      ],
      null,
    )
  }
  selected_model_placements = {
    for model_id, placement in local.catalog_model_placements : model_id => (
      contains(keys(var.deployment.models.pool_overrides), model_id) && placement != null ?
      merge(placement, {
        state               = "customer-tfvars"
        selection_mode      = "exact-pool"
        compatible_pool_ids = [var.deployment.models.pool_overrides[model_id]]
        host_architectures  = local.effective_pool_facts[var.deployment.models.pool_overrides[model_id]].host_architectures
        required_node_labels = {
          "accelerator.fs2.nebius/class"   = local.effective_pool_facts[var.deployment.models.pool_overrides[model_id]].accelerator_class
          "accelerator.fs2.nebius/pool-id" = var.deployment.models.pool_overrides[model_id]
        }
      }) : placement
    )
  }
  scale_from_zero_ephemeral_storage_violations = flatten([
    for model_id, placement in local.selected_model_placements : [
      for pool_id in placement.compatible_pool_ids : format(
        "%s requires %.3f GiB ephemeral storage but pool %s exposes only %.0f GiB in the conservative autoscaler template budget",
        model_id,
        local.selected_model_effective_ephemeral_request_gib[model_id],
        pool_id,
        local.effective_pool_synthetic_ephemeral_budget_gib[pool_id],
        ) if contains(keys(local.effective_pool_facts), pool_id) ? (
        local.effective_pool_facts[pool_id].scale_from_zero &&
        local.selected_model_effective_ephemeral_request_gib[model_id] > local.effective_pool_synthetic_ephemeral_budget_gib[pool_id]
      ) : false
    ]
  ])
  selected_model_replica_ceilings = {
    for model_id, placement in local.selected_model_placements : model_id => try(floor(
      sum([
        for pool_id in placement.compatible_pool_ids :
        try(local.effective_pool_capacities[pool_id].max_nodes, 0) * (
          try(local.effective_pool_facts[pool_id].gpus_per_node, 0)
        )
      ]) / local.model_profile_contract.model_autoscaling_targets[model_id].gpu_count
    ), 0)
  }

  grafana_external_enabled = var.deployment.observability.grafana.publish_external
  modelexpress_managed_nvcr_server_required = (
    var.deployment.acceleration.model_express.enabled &&
    var.deployment.acceleration.model_express.deployment_mode == "managed" &&
    startswith(try(var.deployment.acceleration.model_express.server_image.repository, ""), "nvcr.io/")
  )

  infrastructure_variables = {
    project_id                          = var.deployment.target.project_id
    run_id                              = local.run_id
    cluster_name                        = var.deployment.name
    target_binding                      = local.catalog_target == null || local.target_override_requested ? local.resolved_target_binding : null
    kubernetes_version                  = var.deployment.cluster.kubernetes_version
    control_plane_allowed_cidrs         = sort(tolist(var.deployment.cluster.control_plane_allowed_cidrs))
    capacity_profile                    = local.capacity_profile
    accelerator_pool_profile            = local.accelerator_profile
    gpu_floor_profile                   = "zero"
    accelerator_pool_capacity_overrides = local.accelerator_pool_capacity_overrides
    custom_accelerator_pools            = var.deployment.accelerator_pools
    external_registry_ids               = sort(tolist(var.deployment.artifacts.external_registry_ids))
    registry_delivery = {
      mode              = var.deployment.artifacts.registry_policy.mode
      repository_prefix = var.deployment.artifacts.registry_policy.repository_prefix
      source_hosts      = local.selected_image_source_hosts
    }
    system_pool = var.deployment.cluster.system_pool
    # Elastic general CPU pools, passed through verbatim with their resolved
    # capacity bounds so the stage never re-derives a node count.
    cpu_pools = {
      for pool_id, pool in var.deployment.cpu_pools : pool_id => {
        platform             = pool.platform
        preset               = pool.preset
        capacity_type        = pool.capacity_type
        min_nodes            = local.general_cpu_pool_bounds[pool_id].min_nodes
        max_nodes            = local.general_cpu_pool_bounds[pool_id].max_nodes
        elastic              = local.general_cpu_pool_bounds[pool_id].elastic
        schedulable_capacity = pool.schedulable_capacity
        boot_disk            = pool.boot_disk
        shared_filesystem    = pool.shared_filesystem
        node_labels          = pool.node_labels
        max_surge            = pool.max_surge
        max_unavailable      = pool.max_unavailable
        drain_timeout        = pool.drain_timeout
      }
    }
    shared_cache = var.deployment.storage.shared_cache
    reference_data = {
      enabled = var.deployment.storage.reference_data.enabled
      lifecycle = {
        retention_mode = var.deployment.storage.reference_data.lifecycle.retention_mode
      }
      cpu_pool = {
        platform             = var.deployment.storage.reference_data.cpu_pool.platform
        preset               = var.deployment.storage.reference_data.cpu_pool.preset
        node_count           = var.deployment.storage.reference_data.cpu_pool.node_count
        schedulable_capacity = var.deployment.storage.reference_data.cpu_pool.schedulable_capacity
        boot_disk_type       = var.deployment.storage.reference_data.cpu_pool.boot_disk_type
        boot_disk_gib        = var.deployment.storage.reference_data.cpu_pool.boot_disk_gib
        max_surge            = var.deployment.storage.reference_data.cpu_pool.max_surge
        max_unavailable      = var.deployment.storage.reference_data.cpu_pool.max_unavailable
        drain_timeout        = var.deployment.storage.reference_data.cpu_pool.drain_timeout
      }
      filesystem = {
        size_gib         = var.deployment.storage.reference_data.filesystem.size_gib
        type             = var.deployment.storage.reference_data.filesystem.type
        block_size_bytes = var.deployment.storage.reference_data.filesystem.block_size_bytes
        forbid_deletion  = var.deployment.storage.reference_data.filesystem.forbid_deletion
      }
      object_storage = {
        bucket_name  = local.reference_data_bucket_name
        max_size_gib = var.deployment.storage.reference_data.object_storage.max_size_gib
      }
    }
    scientific_artifacts = {
      enabled = var.deployment.storage.scientific_artifacts.enabled
      lifecycle = {
        retention_mode = var.deployment.storage.scientific_artifacts.lifecycle.retention_mode
      }
      object_storage = {
        bucket_name  = local.scientific_artifacts_bucket_name
        max_size_gib = var.deployment.storage.scientific_artifacts.object_storage.max_size_gib
      }
      retention_days = var.deployment.storage.scientific_artifacts.retention_days
    }
    public_edge_mode         = var.deployment.edge.mode
    public_edge_source_cidrs = sort(tolist(var.deployment.edge.source_cidrs))
    port_forward_local_ports = var.deployment.edge.port_forward_ports
  }

  foundation_variables = {
    grafana_admin_secret_ref = var.deployment.secrets.grafana_admin_secret
    jobset = {
      enabled            = var.deployment.scientific_batch.enabled
      kubernetes_version = var.deployment.cluster.kubernetes_version
    }
    # ModelExpress may request an RDMA device beside the accelerator. Kueue
    # budgets only accelerators, so those auxiliary resources are excluded from
    # quota accounting; accelerator accounting is unchanged.
    kueue = {
      exclude_resource_prefixes = local.kueue_auxiliary_resource_prefixes
      # Removing cpu and memory from the exclusions is what makes a core quota
      # real: while they are excluded Kueue drops the request before admission.
      budget_core_resources = local.root_core_admission_enabled
      # Only meaningful for resources Kueue actually budgets, which the root
      # preflight enforces before this reaches the foundation stage.
      fair_share_resource_weights = var.deployment.scheduling.fair_share_resource_weights
    }
    grafana_publication = {
      enabled           = local.grafana_external_enabled
      external_base_url = ""
    }
    alertmanager = var.deployment.observability.alertmanager
  }

  academic_assets_contract = {
    enabled        = var.academic_assets.enabled
    project_id     = var.deployment.target.project_id
    region         = var.deployment.target.region
    tenant_id      = var.academic_assets.tenant_id
    institution_id = var.academic_assets.institution_id
    namespace      = var.academic_assets.namespace
    runtime_claim = {
      name          = var.academic_assets.runtime_pvc_name
      storage_gib   = var.academic_assets.runtime_storage_gib
      storage_class = var.academic_assets.storage_class
      access_mode   = var.academic_assets.access_mode
      lifecycle     = var.academic_assets.runtime_claim_lifecycle
    }
    legacy_quarantine_claim = {
      enabled     = var.academic_assets.legacy_quarantine.enabled
      namespace   = var.academic_assets.legacy_quarantine.namespace
      name        = var.academic_assets.legacy_quarantine.pvc_name
      storage_gib = var.academic_assets.legacy_quarantine.storage_gib
      retain      = var.academic_assets.legacy_quarantine.retain
    }
    delivery = {
      mode                    = "tenant-private-volume"
      mount_root              = var.academic_assets.mount_root
      asset_gid               = var.academic_assets.asset_gid
      consumer_access         = "supplemental-group"
      world_readable          = false
      embed_licensed_bytes    = false
      general_shared_cache    = false
      deny_egress_on_validate = var.academic_assets.deny_egress_during_offline_validation
    }
    execution                 = var.academic_assets.execution
    assets                    = var.academic_assets.assets
    readiness_manifest_sha256 = var.academic_assets.readiness_manifest_sha256
  }

  workloads_variables = {
    deployment_profile              = local.model_profile
    enabled_model_ids               = local.selected_model_ids
    model_image_overrides           = local.effective_model_images
    model_pool_overrides            = var.deployment.models.pool_overrides
    model_scaling_mode              = var.deployment.models.scaling.mode
    hot_model_ids                   = sort(tolist(var.deployment.models.scaling.hot))
    model_scaling_overrides         = var.deployment.models.scaling.overrides
    keda_polling_interval_seconds   = var.deployment.models.scaling.polling_interval_seconds
    keda_cooldown_period_seconds    = var.deployment.models.scaling.cooldown_period_seconds
    enable_cold_start_keepers       = var.deployment.models.cold_start_keepers
    enable_dcgm_cold_start_campaign = var.deployment.observability.dcgm_cold_start_campaign
    # core_pool_capacity is declared inside the workloads stage's scheduling
    # object and read as var.scheduling.core_pool_capacity, so it must travel
    # inside that object. Emitted as a sibling it was an undeclared variable:
    # Terraform warns and drops it, the stage sees an empty map, and its
    # core-admission precondition fails while the facade believes it supplied
    # the capacity.
    scheduling = merge(var.deployment.scheduling, {
      core_pool_capacity = local.root_core_pool_capacity
    })
    general_cpu_lane = merge(local.general_cpu_lane, {
      namespace = local.general_cpu_namespace
    })
    # One truth, shared with the foundation stage: cpu and memory are budgeted
    # exactly when scheduling.core_capacity is set, because that is what
    # removes them from Kueue's exclusions. Deriving this from the lane
    # instead would let the workloads stage believe its quotas are enforced
    # while the controller still drops core requests before admission. The
    # facade refuses an enabled general CPU lane without core_capacity, so the
    # two can never disagree.
    budget_core_resources                 = local.root_core_admission_enabled
    fast_start_claims                     = var.deployment.storage.fast_start_claims
    accelerator_node_schedulable_capacity = local.root_accelerator_node_sizes
    reference_data = {
      enabled    = var.deployment.storage.reference_data.enabled
      namespace  = var.deployment.storage.reference_data.namespace
      queue      = var.deployment.storage.reference_data.queue
      network    = var.deployment.storage.reference_data.network
      status     = var.deployment.storage.reference_data.status
      pipeline   = var.deployment.storage.reference_data.pipeline
      preprocess = var.deployment.storage.reference_data.preprocess
    }
    scientific_artifacts = {
      enabled               = var.deployment.storage.scientific_artifacts.enabled
      handle_ttl_seconds    = var.deployment.storage.scientific_artifacts.handle_ttl_seconds
      max_artifact_bytes    = var.deployment.storage.scientific_artifacts.max_artifact_bytes
      retention_days        = var.deployment.storage.scientific_artifacts.retention_days
      egress_cidrs          = sort(tolist(var.deployment.storage.scientific_artifacts.egress_cidrs))
      media_types           = sort(tolist(var.deployment.storage.scientific_artifacts.media_types))
      credential_generation = var.deployment.storage.scientific_artifacts.credential_generation
    }
    scientific_batch = {
      enabled                  = var.deployment.scientific_batch.enabled
      writes_enabled           = var.deployment.scientific_batch.writes_enabled
      namespace                = var.deployment.scientific_batch.namespace
      runtime_cache            = var.deployment.scientific_batch.runtime_cache
      execution_map            = local.scientific_execution_map
      workers                  = var.deployment.scientific_batch.workers
      poll_seconds             = var.deployment.scientific_batch.poll_seconds
      lease_seconds            = var.deployment.scientific_batch.lease_seconds
      api_timeout_seconds      = var.deployment.scientific_batch.api_timeout_seconds
      token_expiration_seconds = var.deployment.scientific_batch.token_expiration_seconds
    }
    academic_assets = local.academic_assets_contract
    model_express = {
      enabled         = var.deployment.acceleration.model_express.enabled
      deployment_mode = var.deployment.acceleration.model_express.deployment_mode
      endpoint = (
        var.deployment.acceleration.model_express.deployment_mode == "managed" ?
        "fs2-modelexpress.${var.deployment.acceleration.model_express.namespace}.svc.cluster.local:8001" :
        var.deployment.acceleration.model_express.endpoint
      )
      metadata_backend = var.deployment.acceleration.model_express.metadata_backend
      namespace        = var.deployment.acceleration.model_express.namespace
      server_image     = var.deployment.acceleration.model_express.server_image
      cache            = var.deployment.acceleration.model_express.cache
      external_network = var.deployment.acceleration.model_express.external_network
      models           = var.deployment.acceleration.model_express.models
    }
    acme_email         = var.deployment.edge.acme_email
    acme_environment   = var.deployment.edge.acme_environment
    run_acceptance_job = var.deployment.acceptance.create_probe_job
    control_plane_image = {
      repository = var.deployment.applications.control_plane.repository
      digest     = var.deployment.applications.control_plane.digest
    }
    catalog_rollout_digest = var.deployment.applications.control_plane.catalog_rollout_digest
    admin_console = {
      image = {
        repository = var.deployment.applications.admin_console.repository
        digest     = var.deployment.applications.admin_console.digest
      }
      provenance    = var.deployment.applications.admin_console.provenance
      replica_count = var.deployment.applications.admin_console.replica_count
    }
    admin_observability_links = {
      allowed_hosts = []
      grafana = {
        url                     = ""
        verified_external_route = false
      }
      prometheus   = { url = "", verified_external_route = false }
      loki         = { url = "", verified_external_route = false }
      otel         = { url = "", verified_external_route = false }
      dcgm         = { url = "", verified_external_route = false }
      kueue        = { url = "", verified_external_route = false }
      keda         = { url = "", verified_external_route = false }
      alertmanager = { url = "", verified_external_route = false }
      tempo        = { url = "", verified_external_route = false }
    }
    model_controller = {
      enabled                                    = var.deployment.dynamic_models.enabled
      writes_enabled                             = var.deployment.dynamic_models.writes_enabled
      workload_owner                             = var.deployment.dynamic_models.workload_owner
      bootstrap_model_ids                        = sort(tolist(var.deployment.dynamic_models.bootstrap_model_ids))
      fresh_install                              = var.deployment.dynamic_models.fresh_install
      handoff_receipt                            = var.deployment.dynamic_models.handoff_receipt
      fast_start_evidence_file                   = var.deployment.dynamic_models.fast_start_evidence_file
      fast_start_environment_qualifications_file = var.deployment.dynamic_models.fast_start_environment_qualifications_file
      fast_start_measurement_contracts_file      = var.deployment.dynamic_models.fast_start_measurement_contracts_file
      fast_start_mechanisms_file                 = var.deployment.dynamic_models.fast_start_mechanisms_file
      fast_start_wait_second_value               = var.deployment.dynamic_models.fast_start_wait_second_value
      fast_start_mechanism_hourly_costs          = var.deployment.dynamic_models.fast_start_mechanism_hourly_costs
      priority_classes                           = var.deployment.dynamic_models.priority_classes
    }
  }

  deployment_contract_payload = {
    schema_version = 1
    name           = var.deployment.name
    run_id         = local.run_id
    target = {
      project_id = var.deployment.target.project_id
      region     = var.deployment.target.region
    }
    profiles = {
      capacity     = local.capacity_profile
      accelerators = local.accelerator_profile
      models       = local.model_profile
    }
    selected_accelerator_pool_ids   = sort(keys(local.effective_pool_capacities))
    custom_accelerator_pools        = local.using_custom_accelerator_pools
    selected_model_ids              = local.selected_model_ids
    selected_model_placements       = local.selected_model_placements
    selected_model_replica_ceilings = local.selected_model_replica_ceilings
    admin_configuration = {
      enabled = true
      source  = "derived-terraform-baseline"
    }
    academic_assets = local.academic_assets_contract
    artifact_delivery = {
      mode                  = var.deployment.artifacts.registry_policy.mode
      repository_prefix     = var.deployment.artifacts.registry_policy.repository_prefix
      upstream_registry_ids = sort(tolist(var.deployment.artifacts.external_registry_ids))
      source_hosts          = local.selected_image_source_hosts
    }
    scale_from_zero_storage = {
      boot_disk_allocatable_ratio       = local.managed_autoscaler_boot_disk_allocatable_ratio
      fixed_headroom_gib                = local.managed_autoscaler_ephemeral_headroom_gib
      model_effective_request_gib       = local.selected_model_effective_ephemeral_request_gib
      pool_synthetic_storage_budget_gib = local.effective_pool_synthetic_ephemeral_budget_gib
    }
    stages = {
      infrastructure = local.infrastructure_variables
      foundation     = local.foundation_variables
      workloads      = local.workloads_variables
    }
    secret_environment = {
      grafana_username  = var.deployment.secrets.grafana_username_env
      grafana_password  = var.deployment.secrets.grafana_password_env
      ngc_api_key       = var.deployment.secrets.ngc_api_key_env
      nvcr_dockerconfig = var.deployment.secrets.nvcr_dockerconfig_env
    }
    secret_requirements = {
      grafana_bootstrap = true
      ngc_api_key       = contains(local.selected_model_required_secrets, "ngc_api_key")
      nvcr_dockerconfig = (
        local.model_profile == "full_catalog" ||
        contains(local.selected_model_required_secrets, "nvcr_dockerconfigjson") ||
        local.modelexpress_managed_nvcr_server_required
      )
    }
  }
  deployment_contract = merge(local.deployment_contract_payload, {
    sha256 = sha256(jsonencode(local.deployment_contract_payload))
  })
}
