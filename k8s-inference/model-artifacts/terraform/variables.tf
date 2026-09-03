variable "project_id" {
  description = "Authoritative project from the integrated reference-data deployment."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,63}$", var.project_id))
    error_message = "project_id must be a canonical project identifier."
  }
}

variable "cluster_region" {
  description = "Authoritative region from the integrated reference-data deployment."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,31}$", var.cluster_region))
    error_message = "cluster_region must be a canonical region."
  }
}

variable "cluster_name" {
  description = "Exact Kubernetes context recorded in cache receipts."
  type        = string
  nullable    = false
}

variable "source_commit" {
  description = "Immutable Git commit containing the artifact catalog and renderer."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-f0-9]{8,64}$", var.source_commit))
    error_message = "source_commit must be an immutable hexadecimal Git commit."
  }
}

variable "reference_plane_source_commit" {
  description = "Integrated Git commit that created the reference-data storage and CPU preprocessing plane."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-f0-9]{8,64}$", var.reference_plane_source_commit))
    error_message = "reference_plane_source_commit must be an immutable hexadecimal Git commit."
  }
}

variable "reference_plane_integrated" {
  description = "Explicit deployment gate; true only after the reference-data plan is merged and applied."
  type        = bool
  default     = false
}

variable "public_source_staging_enabled" {
  description = "Whether the integrated reference-data network policy permits checksum-pinned HTTPS downloads."
  type        = bool
  default     = false
}

variable "filesystem_id" {
  description = "Terraform-managed 2 TiB regional reference-data filesystem identifier."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^computefilesystem-[a-z0-9]+$", var.filesystem_id))
    error_message = "filesystem_id must be a canonical managed filesystem identifier."
  }
}

variable "filesystem_size_gib" {
  description = "Observed capacity of the Terraform-managed reference-data filesystem."
  type        = number
  nullable    = false
  validation {
    condition     = floor(var.filesystem_size_gib) == var.filesystem_size_gib && var.filesystem_size_gib >= 2048
    error_message = "Artifact ingestion requires the integrated reference-data filesystem at 2048 GiB or larger."
  }
}

variable "namespace" {
  description = "Dedicated reference-data namespace created by the integrated plan."
  type        = string
  nullable    = false
  validation {
    condition     = var.namespace == "fs2-reference-data"
    error_message = "namespace must be the isolated fs2-reference-data namespace from the integrated plan."
  }
}

variable "local_queue" {
  description = "Dedicated Kueue LocalQueue created by the reference-data plan."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.local_queue)) && length(var.local_queue) <= 63
    error_message = "local_queue must be a Kubernetes DNS label."
  }
}

variable "service_account" {
  description = "Least-privilege service account created by the reference-data plan."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.service_account)) && length(var.service_account) <= 63
    error_message = "service_account must be a Kubernetes DNS label."
  }
}

variable "shared_filesystem_host_path" {
  description = "Node mount path exported by the integrated reference-data storage contract."
  type        = string
  nullable    = false
  validation {
    condition     = startswith(var.shared_filesystem_host_path, "/mnt/") && !strcontains(var.shared_filesystem_host_path, "..")
    error_message = "shared_filesystem_host_path must be a safe absolute path below /mnt."
  }
}

variable "node_selector" {
  description = "Exact selector for the dedicated regular CPU preprocessing pool; the shared system pool is forbidden."
  type        = map(string)
  nullable    = false
  validation {
    condition = (
      lookup(var.node_selector, "workload.fs2.nebius/reference-data", "") == "true" &&
      lookup(var.node_selector, "capacity.fs2.nebius/type", "") == "regular" &&
      lookup(var.node_selector, "capacity.fs2.nebius/pool", "") == "reference-data" &&
      lookup(var.node_selector, "storage.fs2.nebius/reference-data", "") == "true"
    )
    error_message = "node_selector must identify the storage-attached dedicated regular CPU reference-data pool and must not select system."
  }
}

variable "node_toleration" {
  description = "Exact toleration for the dedicated reference-data CPU pool taint."
  type = object({
    key      = string
    operator = string
    value    = string
    effect   = string
  })
  nullable = false
  validation {
    condition = (
      var.node_toleration.key == "workload.fs2.nebius/reference-data" &&
      var.node_toleration.operator == "Equal" &&
      var.node_toleration.value == "true" &&
      var.node_toleration.effect == "NoSchedule"
    )
    error_message = "node_toleration must exactly match the dedicated reference-data CPU taint."
  }
}

variable "cpu_pool_id" {
  description = "Infrastructure-owned node-group ID exported by the integrated reference-data plan."
  type        = string
  nullable    = false
  validation {
    condition     = length(trimspace(var.cpu_pool_id)) > 0
    error_message = "cpu_pool_id is required."
  }
}

variable "cpu_pool_name" {
  description = "Infrastructure-owned node-group name exported by the integrated reference-data plan."
  type        = string
  nullable    = false
  validation {
    condition     = length(trimspace(var.cpu_pool_name)) > 0
    error_message = "cpu_pool_name is required."
  }
}

variable "cache_subpath" {
  description = "Versioned public-model prefix under the integrated reference-data mount."
  type        = string
  default     = "model-artifacts/public/v1"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9./-]*[a-z0-9]$", var.cache_subpath)) && !strcontains(var.cache_subpath, "..")
    error_message = "cache_subpath must be a safe relative path."
  }
}
