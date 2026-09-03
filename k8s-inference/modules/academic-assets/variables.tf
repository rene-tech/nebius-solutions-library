variable "academic_assets" {
  description = <<-EOT
    Tenant-private delivery of licensed academic assets.

    Licensed bytes are mounted read-only from a tenant-private claim and are never
    embedded in an image or placed in a general shared cache.

    Each claim declares its own lifecycle:

      retained    the claim holds licensed bytes that must survive. Terraform will
                  refuse to destroy or replace it, so a long-lived cluster cannot
                  discard verified content by accident.
      disposable  the claim is part of a throwaway acceptance environment and must
                  tear down cleanly with the rest of it.

    Terraform requires prevent_destroy to be a constant, so the two lifecycles are
    separate, mutually exclusive resources selected by this input rather than one
    resource with a computed flag.
  EOT
  type = object({
    enabled        = bool
    project_id     = string
    region         = string
    tenant_id      = string
    institution_id = optional(string, null)
    namespace      = string
    runtime_claim = object({
      name          = string
      storage_gib   = number
      storage_class = string
      access_mode   = string
      lifecycle     = optional(string, "disposable")
    })
    legacy_quarantine_claim = object({
      enabled     = bool
      namespace   = string
      name        = string
      storage_gib = number
      retain      = bool
    })
    delivery = object({
      mode                    = string
      mount_root              = string
      asset_gid               = number
      consumer_access         = string
      world_readable          = bool
      embed_licensed_bytes    = bool
      general_shared_cache    = bool
      deny_egress_on_validate = bool
    })
    assets = map(object({
      model_id              = string
      relative_path         = string
      install_relative_path = optional(string, null)
      read_only             = optional(bool, true)

      # Exactly how a model runtime addresses this object. Without it the module
      # cannot state a mount, so the output reports the binding as absent rather
      # than deriving a path that would be wrong for an installed tree.
      runtime_binding = optional(object({
        artifact_id                = string
        source_sub_path            = string
        consumer_path              = string
        mechanism                  = string
        content_identity_kind      = optional(string, "file-digest")
        content_manifest_algorithm = optional(string, null)
        content_digest_sha256      = optional(string, null)
        size_bytes                 = optional(number, null)
        source_artifact = optional(object({
          filename   = string
          sha256     = string
          size_bytes = number
        }), null)
      }), null)
    }))
    readiness_manifest_sha256 = optional(string, null)
  })
  nullable = false

  validation {
    condition     = contains(["retained", "disposable"], var.academic_assets.runtime_claim.lifecycle)
    error_message = "academic_assets.runtime_claim.lifecycle must be \"retained\" or \"disposable\"."
  }

  validation {
    condition     = var.academic_assets.delivery.embed_licensed_bytes == false
    error_message = "Licensed academic bytes must be mounted from a tenant-private volume, never embedded in an image."
  }

  validation {
    condition     = var.academic_assets.delivery.general_shared_cache == false
    error_message = "Licensed academic bytes must never enter a general shared cache."
  }

  validation {
    condition     = var.academic_assets.delivery.world_readable == false
    error_message = "Licensed academic bytes must never be world-readable; runtimes read them through a supplemental group."
  }

  validation {
    condition     = var.academic_assets.delivery.mode == "tenant-private-volume"
    error_message = "Only tenant-private-volume delivery is supported for licensed academic assets."
  }

  validation {
    condition = (
      var.academic_assets.delivery.asset_gid > 0 &&
      var.academic_assets.delivery.asset_gid < 65536 &&
      var.academic_assets.delivery.consumer_access == "supplemental-group"
    )
    error_message = "Licensed academic bytes are read through a non-root supplemental group."
  }

  validation {
    condition = (
      var.academic_assets.namespace != var.academic_assets.legacy_quarantine_claim.namespace ||
      var.academic_assets.runtime_claim.name != var.academic_assets.legacy_quarantine_claim.name
    )
    error_message = "The canonical runtime claim must be distinct from the retained quarantine claim."
  }

  validation {
    condition     = alltrue([for key, asset in var.academic_assets.assets : asset.read_only])
    error_message = "Runtime pods mount licensed academic assets read-only."
  }

  validation {
    condition = alltrue([
      for key, asset in var.academic_assets.assets :
      asset.runtime_binding == null || (
        startswith(asset.runtime_binding.consumer_path, "/") &&
        !startswith(asset.runtime_binding.source_sub_path, "/") &&
        !strcontains(asset.runtime_binding.source_sub_path, "..") &&
        contains(["subpath-file-mount", "subpath-directory-mount"], asset.runtime_binding.mechanism)
      )
    ])
    error_message = "A runtime binding must localize a safe relative subPath at an absolute consumer path by subPath mount."
  }

  validation {
    condition = alltrue([
      for key, asset in var.academic_assets.assets :
      asset.runtime_binding == null || (
        asset.runtime_binding.content_identity_kind == (
          asset.runtime_binding.mechanism == "subpath-file-mount" ? "file-digest" : "tree-manifest"
        )
      )
    ])
    error_message = "A binding's content identity kind must match how it is mounted: a file by digest, a directory by tree manifest."
  }

  validation {
    condition = alltrue([
      for key, asset in var.academic_assets.assets :
      asset.runtime_binding == null ||
      asset.runtime_binding.content_identity_kind != "tree-manifest" ||
      asset.runtime_binding.source_artifact == null || (
        asset.runtime_binding.content_digest_sha256 != asset.runtime_binding.source_artifact.sha256 &&
        asset.runtime_binding.size_bytes != asset.runtime_binding.source_artifact.size_bytes
      )
    ])
    error_message = "An installed tree must not be labelled with the digest or size of the archive it came from."
  }
}
