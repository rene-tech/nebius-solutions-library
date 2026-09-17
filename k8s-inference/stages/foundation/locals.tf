locals {
  public_edge_enabled = var.public_edge_availability_contract.enabled
  public_edge_availability_contract_sha256 = sha256(jsonencode(
    var.public_edge_availability_contract
  ))
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

  common_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "fs2.nebius.ai/environment"    = "disposable"
    "fs2.nebius.ai/run-id"         = var.run_id
  }

  namespaces = toset([
    "cert-manager",
    "cnpg-system",
    "envoy-gateway-system",
    "fs2-data",
    "fs2-models",
    "fs2-observability",
    "fs2-system",
    "kserve",
    "keda",
    "kueue-system",
    "jobset-system",
  ])

  chart_versions = {
    cert_manager          = "v1.21.1"
    cloudnative_pg        = "0.29.0"
    envoy_gateway         = "v1.8.3"
    filesystem_csi        = "0.1.7"
    keda                  = "2.20.2"
    kueue                 = "0.17.8"
    jobset                = "0.12.0"
    kserve_crd            = "v0.20.0"
    kserve_resources      = "v0.20.0"
    kube_prometheus_stack = "88.5.4"
    loki                  = "7.3.0"
    opentelemetry         = "0.171.0"
    tempo                 = "1.24.4"
  }

  # The chart is materialized once during plan to a content-addressed path
  # under the run root, and the verifier, the CRD apply, and the Helm release
  # all consume that exact archive.
  kueue_chart_archive = data.external.kueue_chart.result.path

  kueue_release = {
    chart_ref            = "oci://registry.k8s.io/kueue/charts/kueue"
    chart_digest         = "sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354"
    chart_digest_ref     = "oci://registry.k8s.io/kueue/charts/kueue@sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354"
    chart_archive_sha256 = "409de6260d2b7834fece5044502822bcb4e74ed8a03b8ea22bb78bcdfa1627db"
    image                = "registry.k8s.io/kueue/kueue:v0.17.8@sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6"
  }

  # "registry/name:v0.17.8@sha256:hex" has three colons, so the repository and
  # the digest-qualified tag are recovered by splitting on "@" first, exactly
  # as the verification script does.
  kueue_image_without_digest = split("@", local.kueue_release.image)[0]
  kueue_image_repository = join(":", slice(
    split(":", local.kueue_image_without_digest),
    0,
    length(split(":", local.kueue_image_without_digest)) - 1,
  ))
  kueue_image_tag = "${element(
    split(":", local.kueue_image_without_digest),
    length(split(":", local.kueue_image_without_digest)) - 1,
  )}@${split("@", local.kueue_release.image)[1]}"

  # values/kueue.yaml is the single source of the pinned image, node placement,
  # feature gates, and controller configuration. Terraform only adds the
  # operator-configured auxiliary resource prefixes, because those depend on
  # what this deployment advertises rather than on the release.
  kueue_values                 = yamldecode(file("${path.module}/values/kueue.yaml"))
  kueue_manager_config         = yamldecode(local.kueue_values.managerConfig.controllerManagerConfigYaml)
  kueue_base_excluded_prefixes = local.kueue_manager_config.resources.excludeResourcePrefixes
  # A ClusterQueue budgets accelerators. Any other extended resource a Pod
  # requests (an RDMA/NIXL device, for example) is not covered by a resource
  # group, and Kueue would refuse to admit the Workload. Excluding those
  # prefixes leaves accelerator accounting untouched.
  kueue_core_resource_prefixes = ["cpu", "memory"]
  kueue_excluded_resource_prefixes = sort(distinct(concat(
    var.kueue.budget_core_resources ? tolist(setsubtract(
      toset(local.kueue_base_excluded_prefixes),
      toset(local.kueue_core_resource_prefixes),
    )) : local.kueue_base_excluded_prefixes,
    var.kueue.exclude_resource_prefixes,
  )))
  kueue_accelerator_resource_names = sort(distinct([
    for pool in values(var.accelerator_pool_contract.pools) : pool.resource_api.resource_name
  ]))
  # Kueue v0.17 sums resource.Quantity magnitudes for usage-based admission
  # fair sharing with a default weight of 1 and no unit normalization, so
  # memory bytes dominate GPU counts once core resources are budgeted. An
  # explicit weight per resource is the only control the API offers; leaving
  # the map empty keeps upstream behaviour untouched rather than guessing.
  kueue_fair_share_resource_weights = var.kueue.fair_share_resource_weights
  kueue_admission_fair_sharing = merge(
    local.kueue_manager_config.admissionFairSharing,
    length(local.kueue_fair_share_resource_weights) == 0 ? {} : {
      resourceWeights = local.kueue_fair_share_resource_weights
    },
  )
  kueue_effective_values = merge(local.kueue_values, {
    managerConfig = {
      controllerManagerConfigYaml = yamlencode(merge(local.kueue_manager_config, {
        resources = merge(local.kueue_manager_config.resources, {
          excludeResourcePrefixes = local.kueue_excluded_resource_prefixes
        })
        admissionFairSharing = local.kueue_admission_fair_sharing
      }))
    }
  })

  edge_gateway_controller_labels = {
    "control-plane" = "envoy-gateway"
  }
  edge_rate_limit_service_labels = {
    "fs2.nebius.ai/edge-rate-limit-service" = "true"
  }
  edge_gateway_controller_pod_scheduling = {
    nodeSelector = local.public_edge_enabled ? var.public_edge_availability_contract.node_selector : {
      "workload.fs2.nebius/system" = "true"
    }
    tolerations = []
    topologySpreadConstraints = [merge({
      maxSkew           = 1
      topologyKey       = var.public_edge_availability_contract.topology_key
      whenUnsatisfiable = local.public_edge_enabled ? "DoNotSchedule" : "ScheduleAnyway"
      labelSelector = {
        matchLabels = local.edge_gateway_controller_labels
      }
    }, local.public_edge_enabled ? {
      minDomains = var.public_edge_availability_contract.minimum_domains
    } : {})]
    affinity = local.public_edge_enabled ? {
      nodeAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = {
          nodeSelectorTerms = [{
            matchFields = [{
              key      = "metadata.name"
              operator = "In"
              values   = local.public_edge_membership_authority.member_instance_ids
            }]
          }]
        }
      }
      podAntiAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = [{
          topologyKey = var.public_edge_availability_contract.topology_key
          labelSelector = {
            matchLabels = local.edge_gateway_controller_labels
          }
        }]
      }
    } : null
  }
  edge_rate_limit_service_pod_scheduling = {
    labels = local.edge_rate_limit_service_labels
    nodeSelector = local.public_edge_enabled ? var.public_edge_availability_contract.node_selector : {
      "workload.fs2.nebius/system" = "true"
    }
    tolerations = []
    topologySpreadConstraints = [merge({
      maxSkew           = 1
      topologyKey       = var.public_edge_availability_contract.topology_key
      whenUnsatisfiable = local.public_edge_enabled ? "DoNotSchedule" : "ScheduleAnyway"
      labelSelector = {
        matchLabels = local.edge_rate_limit_service_labels
      }
    }, local.public_edge_enabled ? {
      minDomains = var.public_edge_availability_contract.minimum_domains
    } : {})]
    affinity = local.public_edge_enabled ? {
      nodeAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = {
          nodeSelectorTerms = [{
            matchFields = [{
              key      = "metadata.name"
              operator = "In"
              values   = local.public_edge_membership_authority.member_instance_ids
            }]
          }]
        }
      }
      podAntiAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = [{
          topologyKey = var.public_edge_availability_contract.topology_key
          labelSelector = {
            matchLabels = local.edge_rate_limit_service_labels
          }
        }]
      }
    } : null
  }
  envoy_gateway_edge_availability_values = {
    deployment = {
      pod = local.edge_gateway_controller_pod_scheduling
    }
    config = {
      envoyGateway = {
        provider = {
          kubernetes = {
            rateLimitDeployment = {
              pod = local.edge_rate_limit_service_pod_scheduling
            }
          }
        }
      }
    }
  }

  kubeconfig                  = yamldecode(file(var.kubeconfig_path))
  selected_context            = try(one([for context in local.kubeconfig.contexts : context if context.name == var.kube_context]), null)
  selected_kubeconfig_cluster = try(local.selected_context.context.cluster, null)
  selected_cluster            = try(one([for cluster in local.kubeconfig.clusters : cluster if cluster.name == local.selected_kubeconfig_cluster]), null)
  selected_api_server           = try(local.selected_cluster.cluster.server, null)
  normalized_run_root           = trimsuffix(abspath(var.run_root), "/")
  expected_kubeconfig_path      = "${local.normalized_run_root}/kubeconfig"
  expected_infrastructure_state = "${local.normalized_run_root}/terraform.tfstate"
  public_edge_system_nodes = local.public_edge_enabled ? try(
    data.kubernetes_resources.public_edge_system_nodes[0].objects,
    [],
  ) : []
  public_edge_system_node_observations_by_name = {
    for node in local.public_edge_system_nodes : try(node.metadata.name, "") => {
      name             = try(node.metadata.name, "")
      uid              = try(node.metadata.uid, "")
      resource_version = try(node.metadata.resourceVersion, "")
      node_group_id    = try(node.metadata.labels["nebius.com/node-group-id"], "")
      run_id           = try(node.metadata.labels["lifecycle.fs2.nebius/run"], "")
      hostname         = try(node.metadata.labels[var.public_edge_availability_contract.topology_key], "")
      ready = try(
        one([
          for condition in node.status.conditions : condition.status
          if condition.type == "Ready"
        ]) == "True",
        false,
      )
      unschedulable = try(node.spec.unschedulable, false)
      blocking_taints = sort([
        for taint in try(node.spec.taints, []) : jsonencode({
          effect = try(taint.effect, "")
          key    = try(taint.key, "")
          value  = try(taint.value, "")
        })
        if contains(
          var.public_edge_availability_contract.scheduler_eligibility.blocking_taint_effects,
          try(taint.effect, ""),
        )
      ])
    }
  }
  public_edge_system_node_observations = [
    for name in sort(keys(local.public_edge_system_node_observations_by_name)) :
    local.public_edge_system_node_observations_by_name[name]
  ]
  public_edge_eligible_system_nodes = [
    for node in local.public_edge_system_node_observations : node
    if node.ready &&
    !node.unschedulable &&
    length(node.name) > 0 &&
    length(node.uid) > 0 &&
    length(node.resource_version) > 0 &&
    length(node.hostname) > 0 &&
    node.node_group_id == var.public_edge_availability_contract.system_node_group_id &&
    node.run_id == var.run_id &&
    length(node.blocking_taints) == 0
  ]
  public_edge_ready_system_node_names = sort([
    for node in local.public_edge_eligible_system_nodes : node.name
  ])
  public_edge_ready_system_hostnames = sort(distinct([
    for node in local.public_edge_eligible_system_nodes : node.hostname
  ]))
  public_edge_eligible_system_node_uids = sort([
    for node in local.public_edge_eligible_system_nodes : node.uid
  ])
  public_edge_ready_node_preflight = {
    required                = local.public_edge_enabled
    selector                = var.public_edge_availability_contract.node_selector
    system_node_group_id    = local.public_edge_enabled ? var.public_edge_availability_contract.system_node_group_id : null
    run_id                  = local.public_edge_enabled ? var.run_id : null
    blocking_taint_effects  = var.public_edge_availability_contract.scheduler_eligibility.blocking_taint_effects
    tolerated_hard_taints   = var.public_edge_availability_contract.scheduler_eligibility.tolerated_hard_taints
    observed_node_count     = length(local.public_edge_system_node_observations)
    eligible_nodes          = local.public_edge_eligible_system_nodes
    eligible_nodes_sha256   = sha256(jsonencode(local.public_edge_eligible_system_nodes))
    eligible_node_uids      = local.public_edge_eligible_system_node_uids
    ready_node_names        = local.public_edge_ready_system_node_names
    ready_node_count        = length(local.public_edge_ready_system_node_names)
    distinct_hostnames      = local.public_edge_ready_system_hostnames
    distinct_hostname_count = length(local.public_edge_ready_system_hostnames)
    required_node_count     = local.public_edge_enabled ? var.public_edge_availability_contract.system_node_count : 0
    required_domains        = local.public_edge_enabled ? var.public_edge_availability_contract.minimum_domains : 0
  }
}
