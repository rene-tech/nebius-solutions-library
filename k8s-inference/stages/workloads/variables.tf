variable "run_root" {
  description = "Absolute mode-0700 lifecycle directory containing kubeconfig and both local state files."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.run_root, "/") && !strcontains(var.run_root, "..")
    error_message = "run_root must be an absolute path without parent traversal."
  }
}

variable "kubeconfig_path" {
  description = "Exact run-owned kubeconfig; must equal <run_root>/kubeconfig."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.kubeconfig_path, "/") && !strcontains(var.kubeconfig_path, "..")
    error_message = "kubeconfig_path must be an absolute path without parent traversal."
  }
}

variable "run_id" {
  description = "Disposable lifecycle ID shared with infrastructure and foundation state."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{5,11}$", var.run_id))
    error_message = "run_id must be 6-12 lowercase alphanumeric characters and start with a letter."
  }
}

variable "cluster_id" {
  description = "Exact disposable Nebius Managed Kubernetes cluster ID."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^mk8scluster-[a-z0-9]+$", var.cluster_id))
    error_message = "cluster_id must be the MK8s ID emitted by this solution's infrastructure state."
  }
}

variable "cluster_name" {
  description = "Exact bounded cluster name emitted by the infrastructure state."
  type        = string
  nullable    = false

  validation {
    condition = (
      length(var.cluster_name) >= 5 &&
      length(var.cluster_name) <= 40 &&
      can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.cluster_name))
    )
    error_message = "cluster_name must be the 5-40 character lowercase DNS-style name emitted by infrastructure."
  }
}

variable "kube_context" {
  description = "Exact kubeconfig context selected by both providers."
  type        = string
  nullable    = false
}

variable "kube_system_uid" {
  description = "Exact kube-system namespace UID captured after infrastructure creation."
  type        = string
  nullable    = false
}

variable "project_id" {
  description = "Exact target project ID. Its region and network identity are bound by target_contract."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = can(regex("^project-[a-z0-9]+$", nonsensitive(var.project_id)))
    error_message = "project_id must be a Nebius project ID; target_contract supplies its validated target identity."
  }
}

variable "target_contract" {
  description = "Exact non-secret target_contract output from infra-disposable. It is authoritative for project, region, network, rollout, tenant, and source-registry identity."
  type = object({
    project_id                 = string
    project_name               = string
    region                     = string
    network_name               = string
    subnet_name                = string
    private_subnet_cidr        = string
    source_registry_project_id = string
    system_update_strategy = object({
      max_surge       = number
      max_unavailable = number
    })
    tenant_id = string
    source_registry = object({
      id         = string
      project_id = string
      fqdn       = string
    })
  })
  nullable = false

  validation {
    condition = try(
      can(regex("^project-[a-z0-9]+$", var.target_contract.project_id)) &&
      length(trimspace(var.target_contract.project_name)) > 0 &&
      can(regex("^[a-z][a-z0-9-]{1,31}[a-z0-9]$", var.target_contract.region)) &&
      length(trimspace(var.target_contract.network_name)) > 0 &&
      length(trimspace(var.target_contract.subnet_name)) > 0 &&
      can(cidrhost(var.target_contract.private_subnet_cidr, 0)) &&
      var.target_contract.private_subnet_cidr != "0.0.0.0/0" &&
      floor(var.target_contract.system_update_strategy.max_surge) == var.target_contract.system_update_strategy.max_surge &&
      floor(var.target_contract.system_update_strategy.max_unavailable) == var.target_contract.system_update_strategy.max_unavailable &&
      var.target_contract.system_update_strategy.max_surge >= 0 &&
      var.target_contract.system_update_strategy.max_unavailable >= 0 &&
      var.target_contract.system_update_strategy.max_surge + var.target_contract.system_update_strategy.max_unavailable >= 1 &&
      can(regex("^tenant-[a-z0-9]+$", var.target_contract.tenant_id)) &&
      can(regex("^registry-[a-z0-9]+$", var.target_contract.source_registry.id)) &&
      can(regex("^project-[a-z0-9]+$", var.target_contract.source_registry.project_id)) &&
      var.target_contract.source_registry_project_id == var.target_contract.source_registry.project_id &&
      length(trimspace(var.target_contract.source_registry.fqdn)) > 0,
      false,
    )
    error_message = "target_contract must be a complete, bounded infra-disposable target and source-registry identity."
  }
}

variable "infrastructure_contract" {
  description = "Optional legacy v1 B300 infrastructure output. accelerator_pool_contract is authoritative; when this compatibility view is supplied it must agree exactly with v2 and foundation."
  type = object({
    schema        = string
    source_commit = string
    target = object({
      project_id = string
      region     = string
      system_update_strategy = object({
        max_surge       = number
        max_unavailable = number
      })
    })
    source_registry = object({
      id         = string
      project_id = string
      fqdn       = string
    })
    capacity = object({
      profile               = string
      floor_profile         = string
      maximum_gpus          = number
      shared_cache_size_gib = number
      system = object({
        capacity        = string
        platform        = string
        preset          = string
        nodes           = number
        max_surge       = number
        max_unavailable = number
      })
      gpu_b300_1x = object({
        capacity      = string
        platform      = string
        preset        = string
        gpus_per_node = number
        min_nodes     = number
        max_nodes     = number
        driver_preset = string
        local_nvme    = bool
      })
      gpu_b300_8x = object({
        capacity      = string
        platform      = string
        preset        = string
        gpus_per_node = number
        min_nodes     = number
        max_nodes     = number
        driver_preset = string
        local_nvme    = bool
      })
    })
  })
  default  = null
  nullable = true

  validation {
    condition = var.infrastructure_contract == null ? true : (
      var.infrastructure_contract.schema == "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1" &&
      can(regex("^[0-9a-f]{40}$", var.infrastructure_contract.source_commit)) &&
      contains(
        keys(jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json")).capacity_profiles),
        var.infrastructure_contract.capacity.profile,
      ) &&
      contains(
        keys(jsondecode(file("${path.module}/../../catalog/profiles/capacity-profiles.json")).floor_profiles),
        var.infrastructure_contract.capacity.floor_profile,
      )
    )
    error_message = "infrastructure_contract must be null or use the reviewed v1 schema, a full source commit, and known capacity/floor profiles."
  }
}

variable "deployment_profile" {
  description = "Model-render profile only. Accelerator identity and capacity are selected independently by accelerator_pool_contract.profile."
  type        = string
  default     = "minimal"

  validation {
    condition = contains(
      keys(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles),
      var.deployment_profile,
    )
    error_message = "deployment_profile must exist in the model-render profile contract."
  }
}

variable "enabled_model_ids" {
  description = "Optional canonical-model subset to render from deployment_profile. Null preserves the complete profile; an explicit empty set renders no model workloads."
  type        = set(string)
  default     = null
  nullable    = true

  validation {
    condition = var.enabled_model_ids == null || length(setsubtract(
      var.enabled_model_ids,
      toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])),
    )) == 0
    error_message = "enabled_model_ids must be null or a subset of the canonical routes in deployment_profile."
  }
}

variable "model_controller" {
  description = "Feature-gated dynamic ModelDeployment controller. Internal envelopes and renderer bundles are derived from the selected catalog, effective accelerator pools, queue, tenant, images, and scaling inputs."
  type = object({
    enabled                           = bool
    writes_enabled                    = bool
    workload_owner                    = string
    bootstrap_model_ids               = set(string)
    fresh_install                     = bool
    handoff_receipt                   = optional(string)
    fast_start_evidence_file          = optional(string)
    fast_start_wait_second_value      = optional(number, 0.01)
    fast_start_mechanism_hourly_costs = optional(map(number), {})
    priority_classes                  = map(number)
  })
  default = {
    enabled             = false
    writes_enabled      = false
    workload_owner      = "terraform"
    bootstrap_model_ids = []
    fresh_install       = false
    handoff_receipt     = null
    priority_classes = {
      interactive = 100
      standard    = 0
      batch       = -100
    }
  }

  validation {
    condition = try(
      contains(["terraform", "released", "controller"], var.model_controller.workload_owner) &&
      (
        var.model_controller.workload_owner == "terraform" ? (
          !var.model_controller.writes_enabled &&
          length(var.model_controller.bootstrap_model_ids) == 0 &&
          !var.model_controller.fresh_install &&
          var.model_controller.handoff_receipt == null
          ) : var.model_controller.workload_owner == "released" ? (
          var.model_controller.enabled &&
          !var.model_controller.writes_enabled &&
          length(var.model_controller.bootstrap_model_ids) == 0 &&
          !var.model_controller.fresh_install &&
          var.model_controller.handoff_receipt == null
          ) : (
          var.model_controller.enabled &&
          var.model_controller.writes_enabled &&
          var.model_scaling_mode == "keda" &&
          (var.model_controller.fresh_install != (var.model_controller.handoff_receipt != null))
        )
      ) &&
      (var.model_controller.enabled || var.model_controller.workload_owner == "terraform") &&
      length(setsubtract(
        var.model_controller.bootstrap_model_ids,
        var.enabled_model_ids == null ?
        toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])) :
        var.enabled_model_ids,
      )) == 0 &&
      (var.model_controller.handoff_receipt == null || can(regex("^sha256:[0-9a-f]{64}$", var.model_controller.handoff_receipt))) &&
      (
        var.model_controller.fast_start_evidence_file == null ? true :
        startswith(pathexpand(var.model_controller.fast_start_evidence_file), "/") &&
        can(jsondecode(file(pathexpand(var.model_controller.fast_start_evidence_file))))
      ) &&
      var.model_controller.fast_start_wait_second_value >= 0 &&
      var.model_controller.fast_start_wait_second_value <= 1000000 &&
      length(var.model_controller.fast_start_mechanism_hourly_costs) <= 128 &&
      alltrue([
        for name, cost in var.model_controller.fast_start_mechanism_hourly_costs :
        can(regex("^[a-z][a-z0-9-]{0,63}$", name)) && cost >= 0
      ]) &&
      length(var.model_controller.priority_classes) > 0 &&
      contains(keys(var.model_controller.priority_classes), "standard") &&
      alltrue([
        for name, value in var.model_controller.priority_classes :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$", name)) &&
        floor(value) == value && value >= -2147483648 && value <= 2147483647
      ]),
      false,
    )
    error_message = "model_controller must preserve one owner; controller mode requires writes, KEDA, a valid bootstrap/handoff; fast-start evidence must be readable JSON at an absolute path; and bounded economic inputs must be valid."
  }
}

variable "model_express" {
  description = "Optional NVIDIA ModelExpress service and exact per-model runtime client declarations. Disabled leaves workloads and infrastructure unchanged."
  type = object({
    enabled          = bool
    deployment_mode  = string
    endpoint         = optional(string)
    metadata_backend = string
    namespace        = string
    server_image = optional(object({
      repository = string
      digest     = string
    }))
    cache = object({
      enabled       = bool
      size_gib      = number
      storage_class = optional(string)
    })
    external_network = optional(object({
      coordinator_namespace  = optional(string)
      coordinator_pod_labels = optional(map(string), {})
      coordinator_cidrs      = optional(set(string), [])
    }), {})
    models = map(object({
      runtime_adapter        = string
      client_package_version = string
      transport = optional(object({
        mode                   = optional(string, "fallback")
        rdma_resource_name     = optional(string)
        rdma_resource_quantity = optional(number, 1)
        nixl_backend           = optional(string, "UCX")
        nic_pin                = optional(string, "auto")
      }), {})
      pool_transports = optional(map(object({
        mode                   = optional(string, "fallback")
        rdma_resource_name     = optional(string)
        rdma_resource_quantity = optional(number, 1)
        nixl_backend           = optional(string, "UCX")
        nic_pin                = optional(string, "auto")
      })), {})
    }))
  })
  default = {
    enabled          = false
    deployment_mode  = "managed"
    endpoint         = null
    metadata_backend = "kubernetes"
    namespace        = "fs2-modelexpress"
    server_image     = null
    cache = {
      enabled       = true
      size_gib      = 100
      storage_class = null
    }
    external_network = {
      coordinator_namespace  = null
      coordinator_pod_labels = {}
      coordinator_cidrs      = []
    }
    models = {}
  }

  validation {
    condition = try(
      !var.model_express.enabled || (
        length(var.model_express.models) > 0 &&
        var.model_controller.enabled &&
        var.model_controller.writes_enabled &&
        var.model_controller.workload_owner == "controller" &&
        var.model_express.endpoint != null &&
        can(regex("^[A-Za-z0-9.-]+:[0-9]{1,5}$", var.model_express.endpoint)) &&
        try(tonumber(element(split(":", var.model_express.endpoint), 1)) >= 1, false) &&
        try(tonumber(element(split(":", var.model_express.endpoint), 1)) <= 65535, false) &&
        (
          var.model_express.deployment_mode == "managed" ? (
            var.model_express.external_network.coordinator_namespace == null &&
            length(var.model_express.external_network.coordinator_pod_labels) == 0 &&
            length(var.model_express.external_network.coordinator_cidrs) == 0
            ) : (
            (
              var.model_express.external_network.coordinator_namespace != null &&
              length(var.model_express.external_network.coordinator_pod_labels) > 0 &&
              length(var.model_express.external_network.coordinator_cidrs) == 0
              ) || (
              var.model_express.external_network.coordinator_namespace == null &&
              length(var.model_express.external_network.coordinator_pod_labels) == 0 &&
              length(var.model_express.external_network.coordinator_cidrs) > 0
            )
          )
        ) &&
        alltrue([
          for cidr in var.model_express.external_network.coordinator_cidrs :
          try("${cidrhost(cidr, 0)}/${element(split("/", cidr), 1)}" == cidr, false)
        ]) &&
        (
          var.model_express.external_network.coordinator_namespace == null ||
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.model_express.external_network.coordinator_namespace))
        ) &&
        alltrue([
          for key, value in var.model_express.external_network.coordinator_pod_labels :
          length(key) >= 1 && length(key) <= 253 && length(value) <= 63
        ]) &&
        length(setsubtract(
          toset(keys(var.model_express.models)),
          var.enabled_model_ids == null ?
          toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])) :
          var.enabled_model_ids,
        )) == 0 &&
        alltrue([
          for model_id, config in var.model_express.models :
          config.runtime_adapter == "vllm" &&
          config.client_package_version == "0.5.1" &&
          try(jsondecode(file("${path.module}/../../catalog/runtime/models/${model_id}.json")).runtime.kind, null) == "vllm" &&
          contains(["fallback", "nixl-rdma"], config.transport.mode) &&
          contains(["UCX", "LIBFABRIC"], config.transport.nixl_backend) &&
          can(regex("^[A-Za-z0-9][A-Za-z0-9_.:,-]*$", config.transport.nic_pin)) &&
          length(config.transport.nic_pin) <= 256 &&
          floor(config.transport.rdma_resource_quantity) == config.transport.rdma_resource_quantity &&
          config.transport.rdma_resource_quantity >= 1 &&
          config.transport.rdma_resource_quantity <= 64 &&
          (
            config.transport.mode == "nixl-rdma" ? (
              config.transport.rdma_resource_name != null &&
              can(regex("^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", config.transport.rdma_resource_name))
              ) : (
              config.transport.rdma_resource_name == null
            )
          ) &&
          alltrue([
            for pool_id, transport in config.pool_transports :
            can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", pool_id)) &&
            length(pool_id) <= 128 &&
            contains(["fallback", "nixl-rdma"], transport.mode) &&
            contains(["UCX", "LIBFABRIC"], transport.nixl_backend) &&
            can(regex("^[A-Za-z0-9][A-Za-z0-9_.:,-]*$", transport.nic_pin)) &&
            length(transport.nic_pin) <= 256 &&
            floor(transport.rdma_resource_quantity) == transport.rdma_resource_quantity &&
            transport.rdma_resource_quantity >= 1 &&
            transport.rdma_resource_quantity <= 64 &&
            (
              transport.mode == "nixl-rdma" ? (
                transport.rdma_resource_name != null &&
                can(regex("^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", transport.rdma_resource_name))
                ) : (
                transport.rdma_resource_name == null
              )
            )
          ])
        ])
      ),
      false,
    )
    error_message = "enabled ModelExpress models require controller ownership, must be selected explicit vLLM runtimes using client 0.5.1, resolve one endpoint and a scoped external coordinator route, and declare fallback or an explicit qualified RDMA extended resource."
  }
}

variable "model_image_overrides" {
  description = "Canonical model ID to deployable OCI image reference. It replaces only placeholder runtime images in that model's manifests."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = (
      length(setsubtract(
        toset(keys(var.model_image_overrides)),
        var.enabled_model_ids == null ?
        toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])) :
        var.enabled_model_ids,
      )) == 0 &&
      alltrue([
        for image in values(var.model_image_overrides) :
        can(regex("^[a-zA-Z0-9._:/@-]+$", image)) && !endswith(split("/", image)[0], ".invalid")
      ])
    )
    error_message = "model_image_overrides must use enabled canonical model IDs and non-placeholder OCI references."
  }
}

variable "model_pool_overrides" {
  description = "Canonical model ID to exact accelerator pool ID. This tfvars-derived map replaces catalog-specific placement labels without changing model manifests or HCL."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = (
      length(setsubtract(
        toset(keys(var.model_pool_overrides)),
        var.enabled_model_ids == null ?
        toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])) :
        var.enabled_model_ids,
      )) == 0 &&
      alltrue([
        for pool_id in values(var.model_pool_overrides) :
        can(regex("^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$", pool_id))
      ])
    )
    error_message = "model_pool_overrides must map enabled canonical model IDs to bounded accelerator pool IDs."
  }
}

variable "model_scaling_mode" {
  description = "Replica owner for routed GPU Deployments. static preserves manifest replicas; keda scales from durable PostgreSQL operation demand."
  type        = string
  default     = "static"

  validation {
    condition     = contains(["static", "keda"], var.model_scaling_mode)
    error_message = "model_scaling_mode must be static or keda."
  }
}

variable "hot_model_ids" {
  description = "Canonical routed models kept at one Ready replica in keda mode. Empty permits every model and GPU node group to reach zero."
  type        = set(string)
  default     = []

  validation {
    condition     = var.model_scaling_mode == "keda" || length(var.hot_model_ids) == 0
    error_message = "hot_model_ids must be empty unless model_scaling_mode is keda."
  }

  validation {
    condition = length(setsubtract(
      var.hot_model_ids,
      var.enabled_model_ids == null ?
      toset(try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, [])) :
      var.enabled_model_ids,
    )) == 0
    error_message = "hot_model_ids must be present in the effective enabled model set."
  }
}

variable "keda_polling_interval_seconds" {
  description = "Seconds between KEDA Prometheus trigger polls while a routed model is at zero replicas."
  type        = number
  default     = 5

  validation {
    condition = (
      floor(var.keda_polling_interval_seconds) == var.keda_polling_interval_seconds &&
      var.keda_polling_interval_seconds >= 1 &&
      var.keda_polling_interval_seconds <= 60
    )
    error_message = "keda_polling_interval_seconds must be an integer from 1 through 60."
  }
}

variable "keda_cooldown_period_seconds" {
  description = "Idle seconds after terminal demand before KEDA scales a non-hot model from one replica to zero."
  type        = number
  default     = 300

  validation {
    condition = (
      floor(var.keda_cooldown_period_seconds) == var.keda_cooldown_period_seconds &&
      var.keda_cooldown_period_seconds >= 5 &&
      var.keda_cooldown_period_seconds <= 7200
    )
    error_message = "keda_cooldown_period_seconds must be an integer from 5 through 7200."
  }
}

variable "keda_fallback_failure_threshold" {
  description = "Deprecated compatibility input. Ignored because a Prometheus outage must not wake every zero-hot model."
  type        = number
  default     = 3

  validation {
    condition = (
      floor(var.keda_fallback_failure_threshold) == var.keda_fallback_failure_threshold &&
      var.keda_fallback_failure_threshold >= 1 &&
      var.keda_fallback_failure_threshold <= 20
    )
    error_message = "keda_fallback_failure_threshold must be an integer from 1 through 20."
  }
}

variable "enable_cold_start_keepers" {
  description = "Render digest-pinned cache/image keepers. DaemonSets do not force a zero-hot GPU pool to scale up."
  type        = bool
  default     = true
}

variable "enable_dcgm_cold_start_campaign" {
  description = "Temporarily collect and scrape only GPU utilization/framebuffer proxy metrics every second for a reviewed cold-start campaign. Defaults to the standard 30-second observability cadence."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_dcgm_cold_start_campaign || var.deployment_profile == "full_catalog"
    error_message = "enable_dcgm_cold_start_campaign requires deployment_profile=full_catalog."
  }
}

variable "public_edge_contract" {
  description = "Exact typed infra-disposable public_edge_contract output. Internal-only mode carries null public identities and a bounded loopback port-forward contract."
  type = object({
    schema                  = string
    mode                    = string
    transport               = string
    public_origin           = optional(string)
    allocation_project_id   = optional(string)
    allocation_id           = optional(string)
    public_ipv4_address     = optional(string)
    external_traffic_policy = string
    service_ports = object({
      http  = object({ listener_port = number, target_port = number, node_port = number })
      https = object({ listener_port = number, target_port = number, node_port = number })
    })
    port_forward = object({
      enabled                  = bool
      bind_address             = optional(string)
      application_origin       = optional(string)
      operator_endpoint        = optional(string)
      operator_proxy_port      = optional(number)
      control_plane_service    = string
      control_plane_port       = number
      control_plane_local_port = optional(number)
      admin_console_service    = string
      admin_console_port       = number
      admin_console_local_port = optional(number)
    })
    security_group_destination_ports = list(number)
  })
  nullable = false

  validation {
    condition = try(
      var.public_edge_contract.schema == "fs2-serve.nebius.ai/public-edge/v1" &&
      contains(["public", "internal-only"], var.public_edge_contract.mode) &&
      var.public_edge_contract.external_traffic_policy == "Cluster" &&
      var.public_edge_contract.service_ports.http.listener_port == 80 &&
      var.public_edge_contract.service_ports.http.target_port == 10080 &&
      var.public_edge_contract.service_ports.https.listener_port == 443 &&
      var.public_edge_contract.service_ports.https.target_port == 10443 &&
      var.public_edge_contract.port_forward.control_plane_service == "fs2-serve-control-plane" &&
      var.public_edge_contract.port_forward.control_plane_port == 8080 &&
      var.public_edge_contract.port_forward.admin_console_service == "fs2-serve-control-plane-admin-console" &&
      var.public_edge_contract.port_forward.admin_console_port == 8080,
      false,
    )
    error_message = "public_edge_contract differs from the reviewed edge/service contract."
  }

  validation {
    condition = var.public_edge_contract.mode != "internal-only" || try(
      var.public_edge_contract.port_forward.enabled &&
      var.public_edge_contract.port_forward.bind_address == "127.0.0.1" &&
      var.public_edge_contract.port_forward.application_origin == format("http://localhost:%d", var.public_edge_contract.port_forward.operator_proxy_port) &&
      var.public_edge_contract.port_forward.operator_endpoint == format("http://127.0.0.1:%d", var.public_edge_contract.port_forward.operator_proxy_port) &&
      alltrue([
        for port in [
          var.public_edge_contract.port_forward.control_plane_local_port,
          var.public_edge_contract.port_forward.admin_console_local_port,
          var.public_edge_contract.port_forward.operator_proxy_port,
        ] : floor(port) == port && port >= 1024 && port <= 65535
      ]) &&
      length(toset([
        var.public_edge_contract.port_forward.control_plane_local_port,
        var.public_edge_contract.port_forward.admin_console_local_port,
        var.public_edge_contract.port_forward.operator_proxy_port,
      ])) == 3,
      false,
    )
    error_message = "An internal-only public_edge_contract requires three distinct non-privileged loopback ports and origins derived from its operator-proxy port."
  }
}

variable "acme_email" {
  description = "Contact for the selected IP ACME Issuer."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.acme_email == null || can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.acme_email))
    error_message = "acme_email must be null or a non-placeholder email address."
  }
}

variable "acme_environment" {
  description = "Let's Encrypt directory used by the chart-owned IP ACME Issuer. Production is the customer-facing default; staging is an explicit test-only tfvars choice."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["staging", "production"], var.acme_environment)
    error_message = "acme_environment must be staging or production."
  }
}

variable "control_plane_image" {
  description = "Immutable control-plane image already containing the matching catalog tree."
  type = object({
    repository = string
    digest     = string
  })
  default = {
    repository = "registry.example.invalid/k8s-inference/control-plane"
    digest     = "sha256:da2624948771c1231b5f70d2420c87f635516b6be0ec5539d8437830d57add55"
  }

  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.control_plane_image.digest))
    error_message = "control_plane_image.digest must be immutable."
  }
}

variable "catalog_rollout_digest" {
  description = "Canonical catalog digest packaged in control_plane_image."
  type        = string
  default     = "sha256:504d87b9aad91a9bb184e7f35e7b8cc8b76595b6ff30637e1ad21d1bb6d4b40f"

  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.catalog_rollout_digest))
    error_message = "catalog_rollout_digest must be a nonzero SHA-256 digest."
  }
}

variable "ngc_api_key" {
  description = "NGC entitlement used by selected NIM models. Required only when model_artifacts marks an enabled model accordingly; stored in disposable local state."
  type        = string
  sensitive   = true
  nullable    = true
  default     = null
}

variable "nvcrio_dockerconfigjson" {
  description = "Docker config JSON for selected nvcr.io model images and the full-catalog DCGM exporter; stored in disposable local state."
  type        = string
  sensitive   = true
  nullable    = true
  default     = null
}

variable "run_acceptance_job" {
  description = "Create a one-shot authenticated HTTPS /v1/models and MCP tools/list probe after the platform is Ready."
  type        = bool
  default     = false
}
