variable "cluster_region" {
  description = "Authoritative region from the existing cluster target contract."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,31}[a-z0-9]$", var.cluster_region))
    error_message = "cluster_region must be a canonical provider region."
  }
}

variable "object_storage_region" {
  description = "Region used by the S3-compatible object endpoint; must equal cluster_region."
  type        = string
  nullable    = false
}

variable "object_bucket_name" {
  description = "Terraform-infrastructure-owned, versioned private bucket name."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.object_bucket_name))
    error_message = "object_bucket_name must be a 3-63 character object-storage bucket name."
  }
}

variable "object_storage_access" {
  description = "Non-secret access-key identity and MysteryBox reference emitted by infrastructure; the secret value is consumed ephemerally."
  type = object({
    access_key_id       = string
    secret_reference_id = string
    revision            = number
  })
  nullable = false

  validation {
    condition = (
      length(var.object_storage_access.access_key_id) >= 8 &&
      can(regex("^[A-Za-z0-9_-]+$", var.object_storage_access.access_key_id)) &&
      can(regex("^[a-z][a-z0-9-]+$", var.object_storage_access.secret_reference_id)) &&
      floor(var.object_storage_access.revision) == var.object_storage_access.revision &&
      var.object_storage_access.revision >= 1
    )
    error_message = "object_storage_access must contain a bounded access-key ID, MysteryBox secret reference and positive revision."
  }
}

variable "namespace" {
  description = "Dedicated namespace for reference-data and preprocessing CPU workloads."
  type        = string
  default     = "fs2-reference-data"
  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.namespace)) && length(var.namespace) <= 63
    error_message = "namespace must be a Kubernetes DNS label."
  }
}

variable "cpu_pool" {
  description = "Infrastructure-owned dedicated regular CPU pool placement and taint contract."
  type = object({
    id         = string
    name       = string
    platform   = string
    preset     = string
    node_count = number
    capacity   = string
    schedulable_capacity = object({
      cpu_millicores        = number
      memory_mib            = number
      ephemeral_storage_mib = number
    })
    node_labels = map(string)
    taint = object({
      key    = string
      value  = string
      effect = string
    })
  })
  nullable = false

  validation {
    condition = (
      var.cpu_pool.capacity == "regular" &&
      var.cpu_pool.node_count >= 1 &&
      floor(var.cpu_pool.schedulable_capacity.cpu_millicores) == var.cpu_pool.schedulable_capacity.cpu_millicores &&
      var.cpu_pool.schedulable_capacity.cpu_millicores >= 1000 &&
      floor(var.cpu_pool.schedulable_capacity.memory_mib) == var.cpu_pool.schedulable_capacity.memory_mib &&
      var.cpu_pool.schedulable_capacity.memory_mib >= 1024 &&
      floor(var.cpu_pool.schedulable_capacity.ephemeral_storage_mib) == var.cpu_pool.schedulable_capacity.ephemeral_storage_mib &&
      var.cpu_pool.schedulable_capacity.ephemeral_storage_mib >= 1024 &&
      var.cpu_pool.node_labels["workload.fs2.nebius/reference-data"] == "true" &&
      var.cpu_pool.node_labels["capacity.fs2.nebius/type"] == "regular" &&
      var.cpu_pool.node_labels["capacity.fs2.nebius/pool"] == "reference-data" &&
      var.cpu_pool.node_labels["storage.fs2.nebius/reference-data"] == "true" &&
      var.cpu_pool.taint.key == "workload.fs2.nebius/reference-data" &&
      var.cpu_pool.taint.value == "true" &&
      var.cpu_pool.taint.effect == "NoSchedule"
    )
    error_message = "cpu_pool must be the infrastructure-owned, storage-attached, tainted regular reference-data pool with positive conservative schedulable capacity."
  }
}

variable "shared_filesystem_host_path" {
  description = <<-EOT
    Existing same-region shared filesystem path already mounted on the dedicated CPU and
    eligible GPU nodes. It must be pre-created and writable by uid/gid 1000.
  EOT
  type        = string
  default     = "/mnt/fs2cache/csi-mounted-fs-path-data/reference-data"
  validation {
    condition     = startswith(var.shared_filesystem_host_path, "/mnt/") && !strcontains(var.shared_filesystem_host_path, "..")
    error_message = "shared_filesystem_host_path must be an absolute safe path below /mnt."
  }
}

variable "queue" {
  description = "Names and bounded CPU/memory quota for the independent Kueue preprocessing lane."
  type = object({
    resource_flavor = optional(string, "reference-data-cpu")
    cluster_queue   = optional(string, "reference-data-cpu")
    local_queue     = optional(string, "reference-data")
    nominal_cpu     = optional(string, "6")
    nominal_memory  = optional(string, "24Gi")
  })
  default = {}
}

variable "status" {
  description = "Optional digest-pinned Python image used only to expose filesystem readiness and Prometheus metrics."
  type = object({
    enabled  = optional(bool, false)
    image    = optional(string)
    replicas = optional(number, 1)
  })
  default = {}
  validation {
    condition = try(
      (!var.status.enabled || can(regex("^[^@[:space:]]+@sha256:[a-f0-9]{64}$", var.status.image))) &&
      floor(var.status.replicas) == var.status.replicas && var.status.replicas >= 1 && var.status.replicas <= 3,
      false,
    )
    error_message = "enabled status requires a digest-pinned image and 1-3 replicas."
  }
}

variable "service_monitor_enabled" {
  description = "Create a Prometheus Operator ServiceMonitor for the status endpoint."
  type        = bool
  default     = false
}

variable "status_ingress_namespaces" {
  description = "Namespace names permitted to read the internal status/metrics service."
  type        = set(string)
  default     = ["fs2-observability", "fs2-system"]
  validation {
    condition = alltrue([
      for name in var.status_ingress_namespaces :
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name)) && length(name) <= 63
    ])
    error_message = "status_ingress_namespaces must contain Kubernetes DNS labels."
  }
}

variable "object_storage_egress_cidrs" {
  description = "Exact CIDRs for the private object endpoint/proxy. Private MSA jobs get no other non-DNS egress."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for cidr in var.object_storage_egress_cidrs : can(cidrnetmask(cidr))])
    error_message = "object_storage_egress_cidrs must contain valid CIDRs."
  }
}

variable "object_storage_egress_fqdns" {
  description = "Exact private object endpoint FQDNs allowed for customer-input preprocessing through Cilium DNS policy."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([
      for name in var.object_storage_egress_fqdns :
      can(regex("^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$", name)) && !strcontains(name, "*")
    ])
    error_message = "object_storage_egress_fqdns must contain exact DNS names without wildcards."
  }
}

variable "allow_public_source_staging" {
  description = "Permit public-source staging Jobs to reach HTTPS sources. This does not permit private MSA Jobs to use public services."
  type        = bool
  default     = false
}

variable "allow_public_msa_opt_in" {
  description = "Create the explicitly labeled public-opt-in egress lane. False is the production default."
  type        = bool
  default     = false
}


variable "pipeline" {
  description = "Optional Kueue-admitted CPU-only official reference-data staging pipeline."
  type = object({
    enabled                 = optional(bool, false)
    bundle_id               = optional(string, "alphafold3-public-databases-v3.0")
    image                   = optional(string)
    generation              = optional(number, 1)
    cpu                     = optional(string, "6")
    memory                  = optional(string, "24Gi")
    ephemeral_storage       = optional(string, "2Gi")
    active_deadline_seconds = optional(number, 604800)
    backoff_limit           = optional(number, 6)
  })
  default = {}

  validation {
    condition = try(
      !var.pipeline.enabled || (
        var.pipeline.bundle_id == "alphafold3-public-databases-v3.0" &&
        can(regex("^[^@[:space:]]+@sha256:[0-9a-f]{64}$", var.pipeline.image)) &&
        floor(var.pipeline.generation) == var.pipeline.generation &&
        var.pipeline.generation >= 1 &&
        can(regex("^[1-9][0-9]*(?:m)?$", var.pipeline.cpu)) &&
        can(regex("^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$", var.pipeline.memory)) &&
        can(regex("^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$", var.pipeline.ephemeral_storage)) &&
        floor(var.pipeline.active_deadline_seconds) == var.pipeline.active_deadline_seconds &&
        var.pipeline.active_deadline_seconds >= 3600 &&
        var.pipeline.active_deadline_seconds <= 1209600 &&
        floor(var.pipeline.backoff_limit) == var.pipeline.backoff_limit &&
        var.pipeline.backoff_limit >= 0 &&
        var.pipeline.backoff_limit <= 20
      ),
      false,
    )
    error_message = "enabled pipeline requires the exact official AlphaFold3 bundle, a digest-pinned image, and bounded CPU-only resources."
  }
}
