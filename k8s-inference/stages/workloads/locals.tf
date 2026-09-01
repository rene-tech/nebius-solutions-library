locals {
  fs2_root            = abspath("${path.module}/../..")
  normalized_run_root = trimsuffix(abspath(var.run_root), "/")

  selected_target        = var.target_contract
  target_contract_sha256 = sha256(jsonencode(var.target_contract))

  capacity_profile_contract              = jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json"))
  legacy_infrastructure_contract_enabled = var.infrastructure_contract != null
  legacy_capacity_profile                = try(var.infrastructure_contract.capacity.profile, null)
  legacy_floor_profile                   = try(var.infrastructure_contract.capacity.floor_profile, null)
  selected_capacity                      = try(local.capacity_profile_contract.capacity_profiles[local.legacy_capacity_profile], null)
  selected_floor                         = try(local.capacity_profile_contract.floor_profiles[local.legacy_floor_profile], null)
  infrastructure_contract_sha256         = local.legacy_infrastructure_contract_enabled ? sha256(jsonencode(var.infrastructure_contract)) : null
  expected_legacy_accelerator_pool_ids   = toset(["nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"])
  expected_infrastructure_contract = !local.legacy_infrastructure_contract_enabled ? null : {
    schema        = "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1"
    source_commit = var.infrastructure_contract.source_commit
    target = {
      project_id = nonsensitive(var.project_id)
      region     = local.selected_target.region
      system_update_strategy = {
        max_surge       = local.selected_target.system_update_strategy.max_surge
        max_unavailable = local.selected_target.system_update_strategy.max_unavailable
      }
    }
    source_registry = {
      id         = local.selected_target.source_registry.id
      project_id = local.selected_target.source_registry.project_id
      fqdn       = local.selected_target.source_registry.fqdn
    }
    capacity = {
      profile               = var.infrastructure_contract.capacity.profile
      floor_profile         = var.infrastructure_contract.capacity.floor_profile
      maximum_gpus          = local.selected_capacity.maximum_gpus
      shared_cache_size_gib = local.selected_capacity.shared_cache_size_gib
      system = {
        capacity        = "regular"
        platform        = "cpu-d3"
        preset          = "8vcpu-32gb"
        nodes           = local.selected_capacity.system_nodes
        max_surge       = local.selected_target.system_update_strategy.max_surge
        max_unavailable = local.selected_target.system_update_strategy.max_unavailable
      }
      gpu_b300_1x = {
        capacity      = "preemptible"
        platform      = "gpu-b300-sxm"
        preset        = "1gpu-24vcpu-346gb"
        gpus_per_node = 1
        min_nodes     = local.selected_floor.gpu_1x_min_nodes
        max_nodes     = local.selected_capacity.gpu_1x_max_nodes
        driver_preset = "cuda13.0"
        local_nvme    = false
      }
      gpu_b300_8x = {
        capacity      = "preemptible"
        platform      = "gpu-b300-sxm"
        preset        = "8gpu-192vcpu-2768gb"
        gpus_per_node = 8
        min_nodes     = local.selected_floor.gpu_8x_min_nodes
        max_nodes     = local.selected_capacity.gpu_8x_max_nodes
        driver_preset = "cuda13.0"
        local_nvme    = true
      }
    }
  }
  legacy_infrastructure_contract_matches_v2 = !local.legacy_infrastructure_contract_enabled || try(
    var.infrastructure_contract.source_commit == var.accelerator_pool_contract.source_commit &&
    var.infrastructure_contract.target.project_id == nonsensitive(var.project_id) &&
    var.infrastructure_contract.target.region == var.accelerator_pool_contract.target_region &&
    var.infrastructure_contract.source_registry.id == var.accelerator_pool_contract.artifact_source.registry.id &&
    var.infrastructure_contract.source_registry.project_id == var.accelerator_pool_contract.artifact_source.registry.project_id &&
    var.infrastructure_contract.source_registry.fqdn == var.accelerator_pool_contract.artifact_source.registry.fqdn &&
    var.infrastructure_contract.capacity.profile == var.accelerator_pool_contract.profile &&
    var.infrastructure_contract.capacity.floor_profile == var.accelerator_pool_contract.floor_profile &&
    var.infrastructure_contract.capacity.maximum_gpus == sum([
      for pool in values(var.accelerator_pool_contract.pools) : pool.node.gpus_per_node * pool.capacity.max_nodes
    ]) &&
    toset(keys(var.accelerator_pool_contract.pools)) == local.expected_legacy_accelerator_pool_ids &&
    length(var.accelerator_pool_contract.capacity_ownership.requested_overrides) == 0 &&
    var.infrastructure_contract.capacity.gpu_b300_1x == {
      capacity      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.type
      platform      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.platform
      preset        = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.preset
      gpus_per_node = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].node.gpus_per_node
      min_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.min_nodes
      max_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity.max_nodes
      driver_preset = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].provider.driver.preset
      local_nvme    = var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].features.local_cache == "local-nvme"
    } &&
    var.infrastructure_contract.capacity.gpu_b300_8x == {
      capacity      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.type
      platform      = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.platform
      preset        = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.preset
      gpus_per_node = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].node.gpus_per_node
      min_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.min_nodes
      max_nodes     = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].capacity.max_nodes
      driver_preset = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].provider.driver.preset
      local_nvme    = var.accelerator_pool_contract.pools["nebius-b300-preemptible-8x"].features.local_cache == "local-nvme"
    },
    false,
  )

  expected_kubeconfig_path    = "${local.normalized_run_root}/kubeconfig"
  expected_foundation_state   = "${local.normalized_run_root}/foundation.tfstate"
  kubeconfig                  = yamldecode(file(var.kubeconfig_path))
  selected_context            = try(one([for context in local.kubeconfig.contexts : context if context.name == var.kube_context]), null)
  selected_kubeconfig_cluster = try(local.selected_context.context.cluster, null)
  selected_cluster            = try(one([for cluster in local.kubeconfig.clusters : cluster if cluster.name == local.selected_kubeconfig_cluster]), null)
  selected_api_server         = try(local.selected_cluster.cluster.server, null)
  kubernetes_api_service_ip   = try(data.kubernetes_service_v1.kubernetes_api.spec[0].cluster_ip, "")
  kubernetes_api_service_cidr = format(
    "%s/%d",
    local.kubernetes_api_service_ip,
    strcontains(local.kubernetes_api_service_ip, ":") ? 128 : 32,
  )
  kubernetes_api_service_cidrs = toset([local.kubernetes_api_service_cidr])
  kubernetes_api_endpoint_ips = toset(flatten([
    for endpoint_slice in data.kubernetes_resources.kubernetes_api_endpoint_slices.objects : flatten([
      for endpoint in try(endpoint_slice.endpoints, []) : [
        for address in try(endpoint.addresses, []) : address
        if try(coalesce(endpoint.conditions.ready, true), true) &&
        contains([for port in try(endpoint_slice.ports, []) : try(port.port, 0)], 443)
      ]
    ])
  ]))
  kubernetes_api_endpoint_cidrs = toset([
    for ip in local.kubernetes_api_endpoint_ips : format(
      "%s/%d",
      ip,
      strcontains(ip, ":") ? 128 : 32,
    )
  ])
  kubernetes_api_egress_cidrs = setunion(
    local.kubernetes_api_service_cidrs,
    local.kubernetes_api_endpoint_cidrs,
    toset([var.target_contract.private_subnet_cidr]),
  )
  public_edge_enabled = var.public_edge_contract.mode == "public"
  public_base_url     = local.public_edge_enabled ? var.public_edge_contract.public_origin : var.public_edge_contract.port_forward.application_origin

  profile_contract = jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json"))
  selected_profile = local.profile_contract.profiles[var.deployment_profile]
  inventory        = jsondecode(file("${local.fs2_root}/components/control-plane/contracts/all-models-live-services.json"))
  runtime_catalog  = jsondecode(file("${local.fs2_root}/catalog/runtime/catalog.json"))
  catalog_models = {
    for model_file in local.runtime_catalog.model_files :
    jsondecode(file("${local.fs2_root}/catalog/runtime/models/${model_file}")).model.id =>
    jsondecode(file("${local.fs2_root}/catalog/runtime/models/${model_file}"))
  }
  catalog_model_runtime_images = {
    for model_id, model in local.catalog_models : model_id => model.runtime.image.reference
  }

  selected_model_ids = sort(tolist(
    var.enabled_model_ids == null ?
    toset(local.selected_profile.canonical_routes) :
    var.enabled_model_ids
  ))
  selected_model_required_secrets = toset(distinct(flatten([
    for model_id in local.selected_model_ids : try(
      local.profile_contract.model_artifacts[model_id].required_secrets,
      [],
    )
  ])))
  ngc_api_key_required = contains(
    local.selected_model_required_secrets,
    "ngc_api_key",
  )
  model_nvcr_credentials_required = contains(
    local.selected_model_required_secrets,
    "nvcr_dockerconfigjson",
  )
  dcgm_nvcr_credentials_required = var.deployment_profile == "full_catalog"
  selected_manifest_paths = sort(distinct(flatten([
    for model_id in local.selected_model_ids : local.profile_contract.model_artifacts[model_id].manifest_paths
  ])))
  selected_model_keeper_paths = sort(distinct(flatten([
    for model_id in local.selected_model_ids : local.profile_contract.model_artifacts[model_id].keeper_paths
  ])))
  selected_keeper_paths = length(local.selected_model_keeper_paths) > 0 ? sort(distinct(concat(
    local.profile_contract.common_keeper_paths,
    local.selected_model_keeper_paths,
  ))) : []
  manifest_source_model_ids = {
    for relative_path in local.selected_manifest_paths : relative_path => [
      for model_id in local.selected_model_ids : model_id
      if contains(local.profile_contract.model_artifacts[model_id].manifest_paths, relative_path)
    ]
  }

  prometheus_server_address   = "http://fs2-${var.run_id}-monitoring-prometheus.fs2-observability.svc.cluster.local:9090"
  dcgm_cadence_contract       = yamldecode(file("${path.module}/values/dcgm-cadence-profiles.yaml"))
  dcgm_cadence_profile        = local.dcgm_cadence_contract.profiles[var.enable_dcgm_cold_start_campaign ? "coldStartCampaign" : "standard"]
  dcgm_collection_interval    = local.dcgm_cadence_profile.attributionMetricCollectionInterval
  dcgm_scrape_interval        = local.dcgm_cadence_profile.helmValues.serviceMonitor.interval
  dcgm_scrape_timeout         = local.dcgm_cadence_profile.helmValues.serviceMonitor.scrapeTimeout
  dcgm_campaign_metrics       = local.dcgm_cadence_contract.campaignMetrics
  dcgm_minimum_nominal_window = local.dcgm_cadence_profile.minimumNominalWindowSeconds
  selected_model_autoscaling_targets = var.model_scaling_mode == "keda" ? {
    for model_id in local.selected_model_ids : model_id => merge(
      local.profile_contract.model_autoscaling_targets[model_id],
      {
        model_id = model_id
        service = merge(
          local.inventory.routes[model_id].service,
          { namespace = local.inventory.namespace },
        )
      },
    )
  } : {}
  model_scalers = {
    for model_id, target in local.selected_model_autoscaling_targets : model_id => merge(target, {
      hot = try(
        var.model_scaling_overrides[model_id].min_replicas > 0,
        contains(var.hot_model_ids, model_id),
      )
      min_replicas = try(
        var.model_scaling_overrides[model_id].min_replicas,
        contains(var.hot_model_ids, model_id) ? 1 : 0,
      )
      max_replicas = try(var.model_scaling_overrides[model_id].max_replicas, 1)
      target_queue_depth = try(
        var.model_scaling_overrides[model_id].target_queue_depth,
        1,
      )
      polling_interval_seconds = try(
        var.model_scaling_overrides[model_id].polling_interval_seconds,
        var.keda_polling_interval_seconds,
      )
      cooldown_seconds = try(
        var.model_scaling_overrides[model_id].cooldown_seconds,
        var.keda_cooldown_period_seconds,
      )
      metric_name      = "fs2_operation_demand_${replace(model_id, "-", "_")}"
      prometheus_query = "max(fs2_serve_operations{model=\"${model_id}\",state=~\"queued|activating|running\"}) OR vector(0)"
    })
  }
  model_autoscaling_config_map_data = var.model_scaling_mode == "keda" ? {
    model_scaling_mode             = "keda"
    model_replica_owner            = "keda"
    activation_handshake           = "disabled-lean-route"
    hot_model_ids_json             = jsonencode(sort(tolist(var.hot_model_ids)))
    model_autoscaling_targets_json = jsonencode({ for model_id, target in local.model_scalers : model_id => target.deployment })
    keda_polling_interval_seconds  = tostring(var.keda_polling_interval_seconds)
    keda_cooldown_period_seconds   = tostring(var.keda_cooldown_period_seconds)
    prometheus_server_address      = local.prometheus_server_address
  } : {}
  autoscaling_target_by_deployment = {
    for model_id, target in local.model_scalers : target.deployment => merge(target, { model_id = model_id })
  }
  catalog_model_placements = {
    for model_id in local.selected_model_ids : model_id => local.profile_contract.workload_placements[
      local.profile_contract.model_autoscaling_targets[model_id].deployment
    ]
  }
  effective_model_placements = {
    for model_id, placement in local.catalog_model_placements : model_id => (
      contains(keys(var.model_pool_overrides), model_id) ?
      merge(placement, {
        state               = "customer-tfvars"
        selection_mode      = "exact-pool"
        compatible_pool_ids = [var.model_pool_overrides[model_id]]
        host_architectures  = local.selected_queue_pools[var.model_pool_overrides[model_id]].node.host_architectures
        required_node_labels = {
          "accelerator.fs2.nebius/class"   = local.selected_queue_pools[var.model_pool_overrides[model_id]].accelerator_class
          "accelerator.fs2.nebius/pool-id" = var.model_pool_overrides[model_id]
        }
      }) : placement
    )
  }

  decoded_model_documents = flatten([
    for relative_path in local.selected_manifest_paths : [
      for index, raw in split("\n---\n", trimspace(file("${local.fs2_root}/${relative_path}"))) : {
        key      = format("%s-%02d-%s-%s", substr(sha256(relative_path), 0, 8), index, lower(yamldecode(raw).kind), yamldecode(raw).metadata.name)
        manifest = yamldecode(raw)
        source   = relative_path
      } if trimspace(raw) != ""
    ]
  ])
  identified_model_documents = [
    for document in local.decoded_model_documents : merge(document, {
      model_id = try(coalesce(
        try(document.manifest.metadata.labels["fs2-serve.nebius.ai/model-id"], null),
        try(document.manifest.metadata.labels["fs2.nebius.ai/model-id"], null),
        contains(
          local.selected_profile.canonical_routes,
          try(document.manifest.metadata.labels["app.kubernetes.io/name"], ""),
        ) ? try(document.manifest.metadata.labels["app.kubernetes.io/name"], null) : null,
        length(local.manifest_source_model_ids[document.source]) == 1 ? local.manifest_source_model_ids[document.source][0] : null,
      ), null)
    })
  ]
  raw_model_documents = [
    for document in local.identified_model_documents : merge(document, {
      gpu_count = document.manifest.kind == "Deployment" ? sum([
        for container in try(document.manifest.spec.template.spec.containers, []) : try(tonumber(container.resources.limits["nvidia.com/gpu"]), 0)
      ]) : 0
      placement = document.manifest.kind == "Deployment" ? try(
        local.effective_model_placements[document.model_id],
        null,
      ) : null
      autoscaled = (
        document.manifest.kind == "Deployment" &&
        contains(keys(local.autoscaling_target_by_deployment), document.manifest.metadata.name)
      )
    }) if document.model_id != null && contains(local.selected_model_ids, document.model_id)
  ]
  autoscaling_target_document_counts = {
    for model_id, target in local.model_scalers : model_id => length([
      for document in local.raw_model_documents : document
      if document.manifest.kind == "Deployment" && document.manifest.metadata.name == target.deployment
    ])
  }
  autoscaling_target_gpu_counts = {
    for model_id, target in local.model_scalers : model_id => sum(flatten([
      for document in local.raw_model_documents : [
        for container in try(document.manifest.spec.template.spec.containers, []) : try(tonumber(container.resources.limits["nvidia.com/gpu"]), 0)
      ] if document.manifest.kind == "Deployment" && document.manifest.metadata.name == target.deployment
    ]))
  }
  image_overridden_model_documents = [
    for document in local.raw_model_documents : merge(document, {
      manifest = jsondecode(
        document.manifest.kind == "Deployment" && contains(keys(var.model_image_overrides), document.model_id) ?
        jsonencode(merge(document.manifest, {
          spec = merge(document.manifest.spec, {
            template = merge(document.manifest.spec.template, {
              spec = merge(
                document.manifest.spec.template.spec,
                {
                  containers = [
                    for container in try(document.manifest.spec.template.spec.containers, []) :
                    (
                      try(container.image, "") == local.catalog_model_runtime_images[document.model_id] ||
                      startswith(
                        try(container.image, ""),
                        "registry.example.invalid/k8s-inference/models/",
                      )
                    ) ?
                    merge(container, { image = var.model_image_overrides[document.model_id] }) :
                    container
                  ]
                },
                length(try(document.manifest.spec.template.spec.initContainers, [])) > 0 ? {
                  initContainers = [
                    for container in document.manifest.spec.template.spec.initContainers :
                    (
                      try(container.image, "") == local.catalog_model_runtime_images[document.model_id] ||
                      startswith(
                        try(container.image, ""),
                        "registry.example.invalid/k8s-inference/models/",
                      )
                    ) ?
                    merge(container, { image = var.model_image_overrides[document.model_id] }) :
                    container
                  ]
                } : {},
              )
            })
          })
        })) :
        jsonencode(document.manifest)
      )
    })
  ]

  placement_overridden_model_documents = [
    for document in local.image_overridden_model_documents : merge(document, {
      manifest = jsondecode(
        document.manifest.kind == "Deployment" && (
          contains(keys(var.model_pool_overrides), document.model_id) ||
          (
            document.gpu_count > 0 &&
            document.placement != null &&
            !alltrue([
              for key, value in document.placement.required_node_labels :
              try(document.manifest.spec.template.spec.nodeSelector[key] == value, false)
            ])
          )
        ) ?
        jsonencode(merge(document.manifest, {
          spec = merge(document.manifest.spec, {
            template = merge(document.manifest.spec.template, {
              spec = merge(document.manifest.spec.template.spec, {
                nodeSelector = contains(keys(var.model_pool_overrides), document.model_id) ? {
                  for key, value in {
                    "accelerator.fs2.nebius/class"   = local.selected_queue_pools[var.model_pool_overrides[document.model_id]].accelerator_class
                    "accelerator.fs2.nebius/pool-id" = var.model_pool_overrides[document.model_id]
                    "kubernetes.io/arch"             = local.selected_queue_pools[var.model_pool_overrides[document.model_id]].node.host_architectures[0]
                  } : key => value
                  if !(
                    local.selected_queue_pools[var.model_pool_overrides[document.model_id]].capacity.scale_from_zero &&
                    contains(
                      local.selected_queue_pools[var.model_pool_overrides[document.model_id]].scheduling.forbidden_scale_zero_selectors,
                      key,
                    )
                  )
                  } : merge(
                  try(document.manifest.spec.template.spec.nodeSelector, {}),
                  document.placement.required_node_labels,
                )
                tolerations = (
                  contains(keys(var.model_pool_overrides), document.model_id) ?
                  local.selected_queue_pools[var.model_pool_overrides[document.model_id]].scheduling.tolerations :
                  local.selected_queue_pools[document.placement.compatible_pool_ids[0]].scheduling.tolerations
                )
              })
            })
          })
        })) : jsonencode(document.manifest)
      )
    })
  ]

  # YAML documents have intentionally heterogeneous object shapes. Encoding
  # both branches before decoding preserves those exact shapes. KEDA-managed
  # Deployments always bootstrap at a stable zero: the ScaledObject, rather
  # than a changing Terraform manifest, establishes any configured hot floor.
  # Static mode leaves every checked-in manifest unchanged.
  model_documents = [
    for document in local.placement_overridden_model_documents : merge(document, {
      manifest = jsondecode(document.autoscaled ? jsonencode(merge(document.manifest, {
        spec = merge(document.manifest.spec, {
          replicas = 0
        })
      })) : jsonencode(document.manifest))
    })
  ]
  model_manifests = { for document in local.model_documents : document.key => document }

  # Keep the cross-contract placement decision evaluable in `terraform
  # console`, then reuse the same result as the resource precondition. A GPU
  # fixture is eligible only when its declared binding is present in the Pod
  # constraints and every compatible pool satisfies the exact selector,
  # toleration, GPU-count, and host-architecture contract.
  model_placement_validations = {
    for document_key, document in local.model_manifests : document_key => (
      document.manifest.kind != "Deployment" ||
      document.gpu_count == 0 ||
      (
        document.placement != null &&
        contains(["fixture-only", "customer-tfvars"], document.placement.state) &&
        document.placement.runtime_variant == "deployment/${document.manifest.metadata.name}" &&
        document.placement.gpu_request == document.gpu_count &&
        alltrue([
          for label, value in document.placement.required_node_labels : (
            try(tostring(document.manifest.spec.template.spec.nodeSelector[label]) == value, false) ||
            anytrue(flatten([
              for term in try(
                document.manifest.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms,
                [],
                ) : [
                for expression in try(term.matchExpressions, []) : (
                  try(expression.key, "") == label &&
                  try(expression.operator, "") == "In" &&
                  try(contains(expression.values, value), false)
                )
              ]
            ]))
          )
        ]) &&
        alltrue([
          for pool_id in document.placement.compatible_pool_ids : (
            contains(keys(local.selected_queue_pools), pool_id) &&
            document.placement.gpu_request <= local.selected_queue_pools[pool_id].node.gpus_per_node &&
            length(setintersection(
              toset(document.placement.host_architectures),
              toset(local.selected_queue_pools[pool_id].node.host_architectures),
            )) > 0 &&
            (
              document.placement.selection_mode == "accelerator-class" ? (
                try(
                  document.placement.required_node_labels["accelerator.fs2.nebius/class"],
                  null,
                ) == local.selected_queue_pools[pool_id].accelerator_class &&
                !contains(
                  keys(document.placement.required_node_labels),
                  "accelerator.fs2.nebius/pool-id",
                )
                ) : (
                document.placement.selection_mode == "exact-pool" &&
                length(document.placement.compatible_pool_ids) == 1 &&
                try(
                  document.placement.required_node_labels["accelerator.fs2.nebius/pool-id"],
                  null,
                ) == pool_id
              )
            ) &&
            alltrue([
              for label, value in document.placement.required_node_labels :
              try(local.selected_queue_pools[pool_id].scheduling.stable_node_labels[label], null) == value
            ]) &&
            alltrue([
              for label, value in try(document.manifest.spec.template.spec.nodeSelector, {}) : (
                label == "kubernetes.io/arch" ?
                contains(local.selected_queue_pools[pool_id].node.host_architectures, tostring(value)) :
                try(local.selected_queue_pools[pool_id].scheduling.stable_node_labels[label], null) == tostring(value)
              )
            ]) &&
            (
              length(try(
                document.manifest.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms,
                [],
              )) == 0 ||
              anytrue([
                for term in try(
                  document.manifest.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms,
                  [],
                  ) : (
                  length(try(term.matchFields, [])) == 0 &&
                  alltrue([
                    for expression in try(term.matchExpressions, []) : (
                      try(expression.operator, "") == "In" &&
                      (
                        try(expression.key, "") == "kubernetes.io/arch" ?
                        length(setintersection(
                          toset(try(expression.values, [])),
                          toset(local.selected_queue_pools[pool_id].node.host_architectures),
                        )) > 0 :
                        try(contains(
                          expression.values,
                          local.selected_queue_pools[pool_id].scheduling.stable_node_labels[expression.key],
                        ), false)
                      )
                    )
                  ])
                )
              ])
            ) &&
            alltrue([
              for required_toleration in local.selected_queue_pools[pool_id].scheduling.tolerations :
              contains(
                [
                  for actual_toleration in try(document.manifest.spec.template.spec.tolerations, []) :
                  jsonencode({
                    effect   = try(actual_toleration.effect, null)
                    key      = try(actual_toleration.key, null)
                    operator = try(actual_toleration.operator, null)
                    value    = try(actual_toleration.value, null)
                  })
                ],
                jsonencode(required_toleration),
              )
            ])
          )
        ])
      )
    )
  }

  keeper_documents = flatten([
    for relative_path in local.selected_keeper_paths : [
      for index, raw in split("\n---\n", trimspace(file("${local.fs2_root}/${relative_path}"))) : {
        key      = format("%s-%02d-%s-%s", substr(sha256(relative_path), 0, 8), index, lower(yamldecode(raw).kind), yamldecode(raw).metadata.name)
        manifest = yamldecode(raw)
        source   = relative_path
      } if trimspace(raw) != ""
    ] if var.enable_cold_start_keepers
  ])
  identified_keeper_documents = [
    for document in local.keeper_documents : merge(document, {
      model_id = try(
        trimspace(split(",", document.manifest.metadata.annotations["fs2-serve.nebius.ai/models"])[0]),
        null,
      )
    })
  ]
  rendered_keeper_documents = [
    for document in local.identified_keeper_documents : merge(document, {
      manifest = jsondecode(
        document.manifest.kind == "DaemonSet" &&
        document.model_id != null &&
        contains(keys(var.model_image_overrides), document.model_id) ?
        jsonencode(merge(document.manifest, {
          spec = merge(document.manifest.spec, {
            template = merge(document.manifest.spec.template, {
              spec = merge(document.manifest.spec.template.spec, {
                containers = [
                  for container in document.manifest.spec.template.spec.containers :
                  (
                    try(container.image, "") == local.catalog_model_runtime_images[document.model_id] ||
                    startswith(
                      try(container.image, ""),
                      "registry.example.invalid/k8s-inference/models/",
                    )
                  ) ?
                  merge(container, { image = var.model_image_overrides[document.model_id] }) :
                  container
                ]
              })
            })
          })
        })) :
        jsonencode(document.manifest)
      )
    })
  ]
  keeper_manifests = { for document in local.rendered_keeper_documents : document.key => document }

  selected_routes = { for model_id in local.selected_model_ids : model_id => local.inventory.routes[model_id] }
  selected_runtime_ports = [
    for port in sort(tolist(toset([
      for model_id in sort(keys(local.selected_routes)) : format("%05d", local.selected_routes[model_id].service.port)
    ]))) : tonumber(port)
  ]
  qualification_projection = jsondecode(file("${local.fs2_root}/components/control-plane/contracts/model-qualification-projection.json"))
  # Terraform routes carry the resolved deployment placement in v4. Reviewed
  # qualification evidence stays beside, but outside, the mounted route file.
  lean_routes = {
    schema = "fs2-serve.nebius.ai/lean-routes/v4"
    routes = [for model_id in sort(keys(local.selected_routes)) : merge(local.selected_routes[model_id], {
      model_id = model_id
      service  = merge(local.selected_routes[model_id].service, { namespace = local.inventory.namespace })
      placement = {
        region            = local.selected_target.region
        accelerator_class = local.effective_model_placements[model_id].required_node_labels["accelerator.fs2.nebius/class"]
        pool_id           = try(local.effective_model_placements[model_id].required_node_labels["accelerator.fs2.nebius/pool-id"], null)
      }
    })]
  }
  lean_routes_config_map_data = {
    "lean-routes.json"              = jsonencode(local.lean_routes)
    "qualification-projection.json" = jsonencode(local.qualification_projection)
  }
  lean_routes_config_map_digest = sha256(jsonencode(local.lean_routes_config_map_data))
  lean_routes_config_map_name   = "fs2-serve-lean-routes-terraform-${substr(local.lean_routes_config_map_digest, 0, 12)}"
  catalog_digest                = trimprefix(var.catalog_rollout_digest, "sha256:")
  serving_bindings = {
    schema         = "fs2-serve.nebius.ai/serving-bindings/v16"
    catalog_digest = local.catalog_digest
    bindings       = {}
  }
  variant_promotions = {
    schema                 = "fs2-serve.nebius.ai/model-variant-promotions/v4"
    route_authority        = "signed-live-evidence-only"
    catalog_digest         = local.catalog_digest
    attestor_policy_sha256 = null
    promotions             = {}
  }

  # Every immutable ConfigMap that changes with the selected catalog is named
  # from its complete data map. Kubernetes can then create the new revision
  # before the control plane switches mounts and Terraform removes the old
  # revision. Fixed names cannot be upgraded safely because immutable objects
  # cannot be updated and create-before-destroy collides with the live name.
  serving_bindings_config_map_data = {
    "serving-bindings.json"         = jsonencode(local.serving_bindings)
    "model-variant-promotions.json" = jsonencode(local.variant_promotions)
  }
  serving_bindings_config_map_digest = sha256(jsonencode(local.serving_bindings_config_map_data))
  serving_bindings_config_map_name   = "fs2-serve-serving-bindings-terraform-${substr(local.serving_bindings_config_map_digest, 0, 12)}"

  platform_contract_config_map_data = merge({
    schema                                      = var.model_scaling_mode == "keda" ? "fs2-serve.nebius.ai/terraform-workloads-contract/v2" : "fs2-serve.nebius.ai/terraform-workloads-contract/v1"
    deployment_profile                          = var.deployment_profile
    canonical_route_count                       = tostring(length(local.selected_model_ids))
    model_manifest_count                        = tostring(length(local.model_manifests))
    keeper_manifest_count                       = tostring(length(local.keeper_manifests))
    catalog_rollout_digest                      = var.catalog_rollout_digest
    keda_scaledobject_count                     = tostring(length(local.model_scalers))
    dcgm_provider_hostengine                    = "present-inactive"
    dcgm_exporter_owner                         = var.deployment_profile == "full_catalog" ? "terraform" : "not-installed-minimal"
    dcgm_exporter_version                       = var.deployment_profile == "full_catalog" ? "4.8.3" : "none"
    dcgm_campaign_enabled                       = tostring(var.enable_dcgm_cold_start_campaign)
    dcgm_attribution_metric_collection_interval = local.dcgm_collection_interval
    dcgm_scrape_interval                        = local.dcgm_scrape_interval
    dcgm_scrape_timeout                         = local.dcgm_scrape_timeout
    run_id                                      = var.run_id
  }, local.model_autoscaling_config_map_data)
  platform_contract_config_map_digest = sha256(jsonencode(local.platform_contract_config_map_data))
  platform_contract_config_map_name   = "fs2-terraform-workloads-contract-${substr(local.platform_contract_config_map_digest, 0, 12)}"

  common_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "fs2.nebius.ai/environment"    = "disposable"
    "fs2.nebius.ai/run-id"         = var.run_id
  }

  control_plane_release_pairs = {
    "sha256:b307083e08ed2e1f556ba97b9beaadb6fcadd1949edb7d2d3ec805d2769c19e8" = "sha256:7d678cdb39a87c8d38c8366a8c47b4b24898df04ecc3f4415853f6be49e2350b"
    "sha256:13406ff0ee5841e76ba0a87f55c0f9b0b9403acd59f1ef8829d5679a3d3c7de5" = "sha256:7d678cdb39a87c8d38c8366a8c47b4b24898df04ecc3f4415853f6be49e2350b"
    "sha256:da2624948771c1231b5f70d2420c87f635516b6be0ec5539d8437830d57add55" = "sha256:504d87b9aad91a9bb184e7f35e7b8cc8b76595b6ff30637e1ad21d1bb6d4b40f"
    "sha256:b48551e3732d62968041b452d43ad49f057dfa57d1aff86d9754371f3999fca9" = "sha256:e00b3acda36808f529dc32ec0db59041d7a716a0122e0fb7573dd217ccf9b694"
  }
}
