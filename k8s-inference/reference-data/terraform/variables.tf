variable "project_id" {
  description = "Nebius project containing the cluster and optional versioned reference-data bucket."
  type        = string
  sensitive   = true
  nullable    = false
}

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

variable "create_object_bucket" {
  description = "Create a task-owned versioned private bucket. False binds an existing private bucket by name without adopting it."
  type        = bool
  default     = false
}

variable "object_bucket_name" {
  description = "Globally unique private bucket name, whether created here or supplied by the integrator."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.object_bucket_name))
    error_message = "object_bucket_name must be a 3-63 character object-storage bucket name."
  }
}

variable "namespace" {
  description = "Dedicated namespace for reference-data and preprocessing CPU workloads."
  type        = string
  default     = "fs2-data"
  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.namespace)) && length(var.namespace) <= 63
    error_message = "namespace must be a Kubernetes DNS label."
  }
}

variable "shared_filesystem_host_path" {
  description = <<-EOT
    Existing same-region shared filesystem path already mounted on system and
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
    nominal_cpu     = optional(string, "32")
    nominal_memory  = optional(string, "128Gi")
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
