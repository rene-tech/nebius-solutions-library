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
