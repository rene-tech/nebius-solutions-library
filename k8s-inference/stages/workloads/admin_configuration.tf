variable "admin_console" {
  description = "Optional immutable static admin UI artifact. Null keeps the UI unpublished."
  type = object({
    image = object({
      repository = string
      digest     = string
    })
    provenance = object({
      source_commit = string
      source_tree   = string
      sbom_sha256   = string
      sbom_format   = optional(string, "cyclonedx-json")
    })
    replica_count = optional(number, 2)
  })
  default  = null
  nullable = true

  validation {
    condition = var.admin_console == null || try(
      can(regex("^[^@]+$", var.admin_console.image.repository)) &&
      !can(regex(":[^/]+$", var.admin_console.image.repository)) &&
      can(regex("^sha256:[a-f0-9]{64}$", var.admin_console.image.digest)) &&
      can(regex("^[a-f0-9]{40}$", var.admin_console.provenance.source_commit)) &&
      can(regex("^[a-f0-9]{40}$", var.admin_console.provenance.source_tree)) &&
      can(regex("^[a-f0-9]{64}$", var.admin_console.provenance.sbom_sha256)) &&
      var.admin_console.provenance.sbom_format == "cyclonedx-json" &&
      floor(var.admin_console.replica_count) == var.admin_console.replica_count &&
      var.admin_console.replica_count >= 1 &&
      var.admin_console.replica_count <= 20,
      false,
    )
    error_message = "admin_console must identify a tag-free repository, immutable image, exact source/SBOM provenance, and 1-20 replicas."
  }
}

variable "admin_configuration" {
  description = "Typed, secret-free Terraform baseline for the admin configuration service. Null disables configuration APIs."
  type = object({
    schema_version = string
    pools = map(object({
      resource_name         = string
      accelerator_class     = string
      capacity_type         = string
      accelerators_per_node = number
      min_nodes             = number
      max_nodes             = number
      node_selector         = map(string)
      tolerations = list(object({
        key                = string
        operator           = string
        value              = optional(string)
        effect             = optional(string)
        toleration_seconds = optional(number)
      }))
    }))
    models = map(object({
      model_id = string
      enabled  = bool
      placement = object({
        pool_ids        = list(string)
        accelerators    = number
        topology_policy = string
      })
      autoscaling = object({
        min_replicas             = number
        max_replicas             = number
        target_queue_depth       = number
        polling_interval_seconds = number
        cooldown_seconds         = number
      })
      queue = object({
        local_queue       = string
        priority_class    = string
        max_queue_seconds = number
      })
      snapshot = object({
        strategy                = string
        cache_tier              = string
        restore_timeout_seconds = number
        parallelism             = number
        require_semantic_check  = bool
      })
      mcp = object({
        exposed   = bool
        tool_name = optional(string)
      })
      rate = object({
        requests_per_minute         = optional(number)
        concurrent_requests         = number
        accelerator_seconds_per_day = optional(number)
      })
      artifact = object({
        image_repository                = string
        image_digest                    = string
        model_revision                  = string
        artifact_manifest_sha256        = optional(string)
        acquisition_contract_sha256     = string
        provenance_sha256               = string
        semantic_health_contract_sha256 = string
      })
    }))
  })
  default  = null
  nullable = true

  validation {
    condition     = var.admin_configuration == null || try(var.admin_configuration.schema_version == "fs2.admin-configuration/v1", false)
    error_message = "admin_configuration must use fs2.admin-configuration/v1."
  }

  validation {
    condition = var.admin_configuration == null || try(
      length(var.admin_configuration.pools) >= 1 &&
      length(var.admin_configuration.pools) <= 128 &&
      length(var.admin_configuration.models) >= 1 &&
      length(var.admin_configuration.models) <= 512 &&
      alltrue([
        for pool_id, pool in var.admin_configuration.pools :
        length(pool_id) >= 1 &&
        length(pool_id) <= 128 &&
        can(regex("^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$", pool.resource_name)) &&
        can(regex("^[a-z][a-z0-9._-]*$", pool.capacity_type)) &&
        floor(pool.accelerators_per_node) == pool.accelerators_per_node &&
        pool.accelerators_per_node >= 1 &&
        pool.accelerators_per_node <= 64 &&
        floor(pool.min_nodes) == pool.min_nodes &&
        floor(pool.max_nodes) == pool.max_nodes &&
        pool.min_nodes >= 0 &&
        pool.max_nodes >= pool.min_nodes
      ]) &&
      alltrue([
        for model_id, model in var.admin_configuration.models :
        model_id == model.model_id &&
        length(model.placement.pool_ids) >= 1 &&
        length(model.placement.pool_ids) == length(toset(model.placement.pool_ids)) &&
        alltrue([for pool_id in model.placement.pool_ids : contains(keys(var.admin_configuration.pools), pool_id)]) &&
        model.autoscaling.min_replicas >= 0 &&
        model.autoscaling.max_replicas >= model.autoscaling.min_replicas &&
        (!model.enabled || model.autoscaling.max_replicas > 0) &&
        contains(["any", "single-node", "nvlink-domain"], model.placement.topology_policy) &&
        contains(["disabled", "cuda-checkpoint", "runtime-native", "weights"], model.snapshot.strategy) &&
        contains(["object-store", "shared-filesystem", "node-local"], model.snapshot.cache_tier) &&
        (model.mcp.exposed == (model.mcp.tool_name != null)) &&
        floor(model.rate.concurrent_requests) == model.rate.concurrent_requests &&
        model.rate.concurrent_requests >= 1 &&
        model.rate.concurrent_requests <= 10000 &&
        (model.rate.requests_per_minute == null ? true : (
          floor(model.rate.requests_per_minute) == model.rate.requests_per_minute &&
          model.rate.requests_per_minute >= 1 &&
          model.rate.requests_per_minute <= 1000000
        )) &&
        (model.rate.accelerator_seconds_per_day == null ? true : (
          floor(model.rate.accelerator_seconds_per_day) == model.rate.accelerator_seconds_per_day &&
          model.rate.accelerator_seconds_per_day >= 1 &&
          model.rate.accelerator_seconds_per_day <= 1000000000
        )) &&
        can(regex("^sha256:[a-f0-9]{64}$", model.artifact.image_digest)) &&
        can(regex("^[a-f0-9]{64}$", model.artifact.acquisition_contract_sha256)) &&
        can(regex("^[a-f0-9]{64}$", model.artifact.provenance_sha256)) &&
        can(regex("^[a-f0-9]{64}$", model.artifact.semantic_health_contract_sha256)) &&
        !strcontains(model.artifact.image_repository, "@") &&
        !can(regex(":[^/]+$", model.artifact.image_repository))
      ]),
      false,
    )
    error_message = "admin_configuration pool, placement, autoscaling, snapshot, MCP, or immutable artifact fields are invalid."
  }
}

variable "admin_configuration_sha256" {
  description = "Canonical JSON SHA-256 of admin_configuration; required with admin_configuration."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.admin_configuration_sha256 == null || can(regex("^[a-f0-9]{64}$", var.admin_configuration_sha256))
    error_message = "admin_configuration_sha256 must be a lowercase SHA-256 value."
  }
}

variable "admin_configuration_plan_id" {
  description = "Optional reviewed admin plan UUID. Null selects authoritative Terraform baseline mode."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.admin_configuration_plan_id == null ||
      can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$", var.admin_configuration_plan_id))
    )
    error_message = "admin_configuration_plan_id must be a canonical UUID."
  }
}

variable "admin_configuration_reconciliation_id" {
  description = "Optional plan-owned durable reconciliation UUID. It must equal admin_configuration_plan_id when supplied."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.admin_configuration_reconciliation_id == null ||
      can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$", var.admin_configuration_reconciliation_id))
    )
    error_message = "admin_configuration_reconciliation_id must be a canonical UUID."
  }
}

variable "admin_configuration_base_revision" {
  description = "Optional durable base revision from the reviewed admin plan. Null selects baseline mode."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition = (
      var.admin_configuration_base_revision == null ||
      (
        floor(var.admin_configuration_base_revision) == var.admin_configuration_base_revision &&
        var.admin_configuration_base_revision >= 1
      )
    )
    error_message = "admin_configuration_base_revision must be a positive integer."
  }
}

variable "admin_configuration_base_etag" {
  description = "Optional durable base ETag from the reviewed admin plan. Null selects baseline mode."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.admin_configuration_base_etag == null ||
      can(regex("^[a-f0-9]{64}$", var.admin_configuration_base_etag))
    )
    error_message = "admin_configuration_base_etag must be a lowercase SHA-256 value."
  }
}

variable "admin_configuration_bootstrap_baseline_accepted" {
  description = "Deprecated compatibility input. Terraform baselines are authoritative whenever no optional reviewed apply receipt is supplied."
  type        = bool
  default     = false
}

variable "model_scaling_overrides" {
  description = "Per-model KEDA values matching the Terraform baseline or optional reviewed admin handoff. Empty preserves the legacy global inputs."
  type = map(object({
    min_replicas             = number
    max_replicas             = number
    target_queue_depth       = number
    polling_interval_seconds = number
    cooldown_seconds         = number
  }))
  default = {}

  validation {
    condition = alltrue([
      for model_id, scaling in var.model_scaling_overrides :
      contains(
        var.enabled_model_ids == null ?
        try(jsondecode(file("${path.module}/../../catalog/profiles/model-profiles.json")).profiles[var.deployment_profile].canonical_routes, []) :
        tolist(var.enabled_model_ids),
        model_id,
      ) &&
      floor(scaling.min_replicas) == scaling.min_replicas &&
      floor(scaling.max_replicas) == scaling.max_replicas &&
      scaling.min_replicas >= 0 &&
      scaling.max_replicas >= scaling.min_replicas &&
      scaling.target_queue_depth >= 1 &&
      scaling.polling_interval_seconds >= 1 &&
      scaling.polling_interval_seconds <= 60 &&
      scaling.cooldown_seconds >= 5 &&
      scaling.cooldown_seconds <= 86400
    ])
    error_message = "model_scaling_overrides must use selected canonical model IDs and bounded integer KEDA settings."
  }
}

locals {
  admin_configuration_enabled         = var.admin_configuration != null
  admin_configuration_json            = local.admin_configuration_enabled ? jsonencode(var.admin_configuration) : null
  admin_configuration_computed_sha256 = local.admin_configuration_enabled ? sha256(local.admin_configuration_json) : null
  admin_configuration_receipt_values = [
    var.admin_configuration_plan_id,
    var.admin_configuration_reconciliation_id,
    var.admin_configuration_base_revision,
    var.admin_configuration_base_etag,
  ]
  admin_configuration_receipt_enabled = alltrue([
    for value in local.admin_configuration_receipt_values : value != null
  ])
  admin_configuration_receipt = local.admin_configuration_receipt_enabled ? {
    schema_version       = "fs2.admin-terraform-apply/v1"
    plan_id              = var.admin_configuration_plan_id
    reconciliation_id    = var.admin_configuration_reconciliation_id
    base_revision        = var.admin_configuration_base_revision
    base_etag            = var.admin_configuration_base_etag
    proposed_etag        = local.admin_configuration_computed_sha256
    configuration_sha256 = local.admin_configuration_computed_sha256
  } : null
  admin_configuration_name = local.admin_configuration_enabled ? format(
    "fs2-admin-configuration-%s-%s",
    substr(local.admin_configuration_computed_sha256, 0, 16),
    local.admin_configuration_receipt_enabled ? substr(replace(var.admin_configuration_plan_id, "-", ""), 0, 8) : "baseline",
  ) : null

  admin_control_plane_overrides = merge(
    var.admin_console == null ? {} : {
      adminConsole = {
        enabled      = true
        replicaCount = var.admin_console.replica_count
        image = {
          repository  = var.admin_console.image.repository
          digest      = var.admin_console.image.digest
          pullPolicy  = "IfNotPresent"
          pullSecrets = []
        }
        provenance = {
          sourceCommit = var.admin_console.provenance.source_commit
          sourceTree   = var.admin_console.provenance.source_tree
          sbomSha256   = var.admin_console.provenance.sbom_sha256
          sbomFormat   = var.admin_console.provenance.sbom_format
        }
        httpRoute = {
          enabled               = local.public_edge_enabled
          requestTimeout        = "30s"
          backendRequestTimeout = "30s"
        }
      }
    },
    local.admin_configuration_enabled ? {
      adminConfiguration = {
        enabled       = true
        configMapName = local.admin_configuration_name
        key           = "admin-configuration.json"
        receiptKey    = local.admin_configuration_receipt_enabled ? "terraform-apply-receipt.json" : ""
        sha256        = local.admin_configuration_computed_sha256
      }
    } : {},
  )
}

resource "kubernetes_config_map_v1" "admin_configuration" {
  count = local.admin_configuration_enabled ? 1 : 0

  metadata {
    name      = local.admin_configuration_name
    namespace = "fs2-system"
    labels    = local.common_labels
    annotations = {
      "fs2-serve.nebius.ai/configuration-sha256" = local.admin_configuration_computed_sha256
      "fs2-serve.nebius.ai/configuration-owner"  = local.admin_configuration_receipt_enabled ? "terraform-reviewed-handoff" : "terraform-baseline"
      "fs2-serve.nebius.ai/configuration-plan"   = local.admin_configuration_receipt_enabled ? var.admin_configuration_plan_id : "none"
    }
  }

  immutable = true
  data = merge(
    { "admin-configuration.json" = local.admin_configuration_json },
    local.admin_configuration_receipt_enabled ? {
      "terraform-apply-receipt.json" = jsonencode(local.admin_configuration_receipt)
    } : {},
  )

  lifecycle {
    create_before_destroy = true

    precondition {
      condition = (
        var.admin_configuration_sha256 != null &&
        var.admin_configuration_sha256 == local.admin_configuration_computed_sha256
      )
      error_message = "admin_configuration_sha256 must equal Terraform's canonical JSON digest."
    }

    precondition {
      condition = (
        alltrue([for value in local.admin_configuration_receipt_values : value == null]) ||
        local.admin_configuration_receipt_enabled
      )
      error_message = "Optional Terraform apply receipt inputs must be supplied together or all omitted for baseline mode."
    }

    precondition {
      condition = (
        !local.admin_configuration_receipt_enabled ||
        (
          var.admin_configuration_plan_id == var.admin_configuration_reconciliation_id &&
          var.admin_configuration_base_etag != local.admin_configuration_computed_sha256
        )
      )
      error_message = "Terraform apply receipt must use one plan-owned reconciliation ID and describe a real ETag change."
    }

    precondition {
      condition = (
        length(setsubtract(
          toset(keys(var.admin_configuration.models)),
          toset(local.selected_model_ids),
        )) == 0 &&
        length(setsubtract(
          toset(local.selected_model_ids),
          toset(keys(var.admin_configuration.models)),
        )) == 0
      )
      error_message = "admin_configuration models must exactly match the tfvars-selected canonical routes."
    }

    precondition {
      condition = (
        !local.admin_configuration_receipt_enabled ||
        var.model_scaling_mode == "keda"
      )
      error_message = "A reviewed admin configuration change requires KEDA to remain the sole routed Deployment replica owner; a receipt-free baseline may truthfully describe a static deployment."
    }

    precondition {
      condition = jsonencode(var.model_scaling_overrides) == jsonencode({
        for model_id, model in var.admin_configuration.models : model_id => {
          min_replicas             = model.autoscaling.min_replicas
          max_replicas             = model.autoscaling.max_replicas
          target_queue_depth       = model.autoscaling.target_queue_depth
          polling_interval_seconds = model.autoscaling.polling_interval_seconds
          cooldown_seconds         = model.autoscaling.cooldown_seconds
        }
      })
      error_message = "model_scaling_overrides must exactly match admin_configuration."
    }

    precondition {
      condition = (
        !local.admin_configuration_receipt_enabled ||
        length(setsubtract(var.hot_model_ids, toset([
          for model_id, model in var.admin_configuration.models : model_id
          if model.enabled && model.autoscaling.min_replicas > 0
        ]))) == 0 &&
        length(setsubtract(toset([
          for model_id, model in var.admin_configuration.models : model_id
          if model.enabled && model.autoscaling.min_replicas > 0
        ]), var.hot_model_ids)) == 0
      )
      error_message = "hot_model_ids must equal the nonzero model floors in admin_configuration."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

output "admin_configuration_contract" {
  description = "Secret-free immutable Terraform baseline with an optional reviewed apply receipt."
  value = {
    schema                 = "fs2-serve.nebius.ai/admin-configuration-terraform/v1"
    enabled                = local.admin_configuration_enabled
    configuration_sha256   = local.admin_configuration_computed_sha256
    config_map_name        = local.admin_configuration_name
    browser_cloud_mutation = false
    reconciliation_state = !local.admin_configuration_enabled ? "disabled" : (
      local.admin_configuration_receipt_enabled ? "terraform-reviewed" : "terraform-baseline"
    )
    infrastructure_change_owner = "terraform"
    accelerator_pools           = local.admin_configuration_enabled ? var.admin_configuration.pools : {}
    model_scaling_overrides     = var.model_scaling_overrides
    apply_receipt = local.admin_configuration_receipt_enabled ? {
      plan_id           = var.admin_configuration_plan_id
      reconciliation_id = var.admin_configuration_reconciliation_id
      base_revision     = var.admin_configuration_base_revision
      base_etag         = var.admin_configuration_base_etag
      proposed_etag     = local.admin_configuration_computed_sha256
    } : null
    bootstrap_baseline = {
      source                                 = "immutable-terraform-configmap"
      accepted_without_prior_revision        = local.admin_configuration_enabled && !local.admin_configuration_receipt_enabled
      static_equivalence_proven_by_plan      = false
      static_equivalence_acceptance_required = false
      arbitrary_api_bootstrap_allowed        = false
      authoritative_on_startup               = local.admin_configuration_enabled && !local.admin_configuration_receipt_enabled
      durable_revision_adoption              = local.admin_configuration_enabled && !local.admin_configuration_receipt_enabled
    }
  }
}
