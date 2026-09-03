variable "kubeconfig_path" {
  description = "Absolute path to the run-scoped mode-0600 kubeconfig created for this disposable cluster."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.kubeconfig_path, "/") && !strcontains(var.kubeconfig_path, "..")
    error_message = "kubeconfig_path must be an absolute path without parent traversal."
  }
}

variable "run_root" {
  description = "Absolute run-owned directory. The selected kubeconfig must be exactly <run_root>/kubeconfig."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.run_root, "/") && !strcontains(var.run_root, "..")
    error_message = "run_root must be an absolute path without parent traversal."
  }
}

variable "run_id" {
  description = "Disposable lifecycle identifier used by the infrastructure state."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{5,11}$", var.run_id))
    error_message = "run_id must be 6-12 lowercase alphanumeric characters and start with a letter."
  }
}

variable "cluster_id" {
  description = "Exact Nebius Managed Kubernetes cluster ID emitted by the reviewed infrastructure state."
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
  description = "Exact kubeconfig context selected by both Kubernetes and Helm providers."
  type        = string
  nullable    = false
}

variable "kube_system_uid" {
  description = "Expected kube-system namespace UID captured from this new cluster after credential generation."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{20,}$", var.kube_system_uid))
    error_message = "kube_system_uid must be the exact Kubernetes namespace UID, not a placeholder."
  }
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
  description = "Optional legacy v1 B300 infrastructure output. accelerator_pool_contract is authoritative; when this compatibility view is supplied it must agree exactly with v2."
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

variable "grafana_admin_secret_ref" {
  description = "Reference to a pre-created Grafana admin Secret. Secret values never enter Terraform."
  type = object({
    name         = string
    user_key     = string
    password_key = string
  })
  nullable = false
}

variable "bootstrap_grafana_credentials" {
  description = "Optional credentials used only for a disposable fresh-cluster bootstrap. They enter the local foundation state. Leave null when the referenced Secret is provisioned externally."
  type = object({
    username = string
    password = string
  })
  sensitive = true
  nullable  = true
  default   = null
}

variable "kueue" {
  description = "Kueue configuration this deployment adds to the pinned release values."
  type = object({
    # Extended-resource prefixes Kueue must not budget, such as an RDMA device
    # a model runtime requests alongside its accelerator.
    exclude_resource_prefixes = optional(list(string), [])
    # When true, cpu and memory leave the exclusions so Kueue counts core
    # requests. ephemeral-storage stays excluded because no ClusterQueue here
    # budgets it.
    budget_core_resources = optional(bool, false)
    # Per-resource weights for usage-based admission fair sharing. Kueue
    # v0.17 sums resource.Quantity magnitudes with a default weight of 1 per
    # resource and applies no unit normalization, so once cpu and memory are
    # budgeted a single Workload's memory bytes exceed any plausible GPU count
    # by nine orders of magnitude and fair-share ordering stops tracking
    # accelerator demand. Setting a weight per resource is the only control
    # Kueue offers. Empty leaves upstream behaviour exactly as it is.
    fair_share_resource_weights = optional(map(number), {})
  })
  default = {}

  validation {
    # Kueue matches these with a literal prefix comparison against the whole
    # ResourceName, so a qualified name such as example.com/rdma_shared_device_a
    # is a valid entry and must not be rejected as a DNS-only string.
    condition = alltrue([
      for prefix in var.kueue.exclude_resource_prefixes :
      length(prefix) >= 1 &&
      length(prefix) <= 317 &&
      length(split("/", prefix)) <= 2 &&
      length(split("/", prefix)[0]) <= 253 &&
      # An unqualified literal prefix has no name half to bound.
      (length(split("/", prefix)) == 1 ? true : length(element(split("/", prefix), 1)) <= 63) &&
      # A literal prefix may stop at the slash ("networking.example.com/"), be
      # a partial name, or be a complete ResourceName.
      can(regex("^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?(?:/(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]*)?)?)?$", prefix))
    ])
    error_message = "Kueue excluded resource prefixes must be bounded literal ResourceName prefixes: an optional <=253 character DNS-style prefix and an optional <=63 character name after a single slash."
  }

  validation {
    condition = (
      length(var.kueue.fair_share_resource_weights) == 0 ||
      (
        alltrue([
          for resource_name, weight in var.kueue.fair_share_resource_weights :
          weight >= 0 &&
          length(resource_name) <= 317 &&
          can(regex("^([a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", resource_name))
        ]) &&
        # Kueue defaults an unspecified resource to weight 1, so a policy that
        # weights cpu or memory without weighting an accelerator leaves the
        # dominant term untouched, and one that budgets core resources must
        # name both of them.
        (
          length(setintersection(
            toset(keys(var.kueue.fair_share_resource_weights)),
            toset(["cpu", "memory"]),
          )) == 0 ||
          (
            length(setsubtract(
              toset(keys(var.kueue.fair_share_resource_weights)),
              toset(["cpu", "memory", "ephemeral-storage"]),
            )) >= 1 &&
            length(setintersection(
              toset(keys(var.kueue.fair_share_resource_weights)),
              toset(["cpu", "memory"]),
            )) == 2
          )
        ) &&
        (
          !var.kueue.budget_core_resources ||
          length(setintersection(
            toset(keys(var.kueue.fair_share_resource_weights)),
            toset(["cpu", "memory"]),
          )) == 2
        ) &&
        anytrue([
          for weight in values(var.kueue.fair_share_resource_weights) : weight > 0
        ])
      )
    )
    error_message = "A fair-share resource weight policy must be empty or complete: Kueue defaults every unspecified resource to weight 1, so a partial map leaves the omitted terms at their raw magnitudes. Keys must be valid ResourceNames with nonnegative weights and at least one positive; a policy that weights cpu or memory must weight both and at least one accelerator resource, and with core resources budgeted cpu and memory must both appear."
  }
}

variable "jobset" {
  description = "Pinned JobSet foundation required by enabled scientific true-gang execution."
  type = object({
    enabled            = optional(bool, false)
    kubernetes_version = optional(string, "1.35")
  })
  default = {}

  validation {
    condition = try(
      tonumber(split(".", trimprefix(var.jobset.kubernetes_version, "v"))[0]) == 1 &&
      length(split(".", trimprefix(var.jobset.kubernetes_version, "v"))) >= 2 &&
      length(split(".", trimprefix(var.jobset.kubernetes_version, "v"))) <= 3 &&
      contains(
        [33, 34, 35],
        tonumber(split(".", trimprefix(var.jobset.kubernetes_version, "v"))[1]),
      ) &&
      (!var.jobset.enabled || contains(
        [33, 34],
        tonumber(split(".", trimprefix(var.jobset.kubernetes_version, "v"))[1]),
      )),
      false,
    )
    error_message = "Kueue v0.17.8's own end-to-end matrix covers Kubernetes 1.33-1.35; JobSet v0.12.0's covers 1.32-1.34. Enabling JobSet therefore requires their tested intersection, 1.33 or 1.34."
  }
}
