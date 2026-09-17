# Dedicated scientific result artifact store.
#
# This is deliberately a second, separate object store. The reference-data
# bucket holds immutable public science inputs that are expensive to rebuild;
# this bucket holds tenant result bytes with a different retention, a different
# writer identities and a different blast radius. Neither bucket's policy or
# lifecycle is widened to serve the other.
#
# Every tenant has a distinct provider principal and access key scoped to its
# exact canonical prefix. Each key is delivered only to that tenant's isolated
# broker workload; neither the shared gateway nor any broker process can read
# the complete tenant key set.

locals {
  scientific_artifacts_enabled = var.scientific_artifacts.enabled
  scientific_artifacts_retain  = local.scientific_artifacts_enabled && var.scientific_artifacts.lifecycle.retention_mode == "retain"
  scientific_artifacts_dispose = local.scientific_artifacts_enabled && var.scientific_artifacts.lifecycle.retention_mode == "disposable"

  # The canonical object root. Every committed artifact is addressed by tenant,
  # operation, stage, shard, attempt, direction and content digest, so one
  # tenant's prefix can never overlap another's and a rerun never overwrites a
  # committed object.
  scientific_artifacts_root = "scientific/v1"
  # A broker may create a new immutable provider version and read/list exact
  # versions, but it must never possess DeleteObject.  In combination with
  # bucket versioning and the absence of noncurrent-version expiration below,
  # a stolen tenant credential can make a newer current version but cannot
  # destroy the finalized VersionId pinned in PostgreSQL.  This is the
  # provider-supported write-once-equivalent boundary while Object Lock is not
  # exposed by the Nebius bucket Terraform resource.
  scientific_artifacts_object_roles = [
    "storage.uploader",
    "storage.object-viewer",
    "storage.object-lister",
  ]
  scientific_artifacts_legacy_authorized = (
    local.scientific_artifacts_enabled &&
    var.scientific_artifacts.migration.phase == "legacy-overlap"
  )
  scientific_artifact_tenants = local.scientific_artifacts_enabled ? {
    for tenant_id in var.scientific_artifacts.tenant_ids : tenant_id => {
      name  = "${local.resource_name}-sci-${substr(sha256(tenant_id), 0, 32)}"
      path  = "scientific/v1/tenants/${tenant_id}/*"
    }
  } : {}
  scientific_artifact_generation_policy = local.scientific_artifacts_enabled ? var.scientific_artifacts.credential_generations : {}
  scientific_artifact_authorized_generations = merge([
    for tenant_id, policy in local.scientific_artifact_generation_policy : {
      for generation in policy.authorized_generations : "${tenant_id}:${generation}" => {
        tenant_id  = tenant_id
        generation = generation
        path       = local.scientific_artifact_tenants[tenant_id].path
      }
    }
  ]...)
  # Generation 1 intentionally remains under the original per-tenant resource
  # addresses. Later generations are additive resources. Removing a generation
  # from this map is forbidden by the input contract and every identity carries
  # prevent_destroy; rotation changes only the bucket's active group.
  scientific_artifact_additional_generations = merge([
    for tenant_id, policy in local.scientific_artifact_generation_policy : {
      for generation in policy.retained_generations : "${tenant_id}:${generation}" => {
        tenant_id  = tenant_id
        generation = generation
        name        = "${local.scientific_artifact_tenants[tenant_id].name}-g${generation}"
        path        = local.scientific_artifact_tenants[tenant_id].path
      } if generation > 1
    }
  ]...)

  # Storage-side hygiene only. Expiring a *current* object is an application
  # decision made against the durable result record, so no rule here deletes
  # live artifacts. In particular, no lifecycle rule may expire a noncurrent
  # version: exact finalized VersionIds must survive a compromised uploader
  # making a newer version for at least the complete application retention
  # window. These rules reclaim only incomplete multipart parts and empty
  # delete markers left by separately authorized retention maintenance.
  scientific_artifacts_lifecycle_rules = [
    {
      id                                = "abort-incomplete-multipart-uploads"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = { days_after_initiation = 1 }
      expiration                        = null
      noncurrent_version_expiration     = null
      noncurrent_version_transition     = null
      transition                        = null
    },
    {
      id                                = "remove-expired-delete-markers"
      status                            = "ENABLED"
      abort_incomplete_multipart_upload = null
      expiration                        = { expired_object_delete_marker = true, days = null, date = null }
      noncurrent_version_expiration     = null
      noncurrent_version_transition     = null
      transition                        = null
    },
  ]

  scientific_artifacts_bucket_id = local.scientific_artifacts_enabled ? (
    local.scientific_artifacts_retain ?
    one(nebius_storage_v1_bucket.scientific_artifacts[*].id) :
    one(nebius_storage_v1_bucket.scientific_artifacts_disposable[*].id)
  ) : null
  scientific_artifacts_bucket_name = local.scientific_artifacts_enabled ? (
    local.scientific_artifacts_retain ?
    one(nebius_storage_v1_bucket.scientific_artifacts[*].name) :
    one(nebius_storage_v1_bucket.scientific_artifacts_disposable[*].name)
  ) : null
  scientific_artifacts_endpoint = "https://storage.${local.selected_target.region}.nebius.cloud"
}

resource "terraform_data" "scientific_artifacts_contract" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  input = {
    bucket_name     = var.scientific_artifacts.object_storage.bucket_name
    max_size_gib    = var.scientific_artifacts.object_storage.max_size_gib
    retention_mode  = var.scientific_artifacts.lifecycle.retention_mode
    retention_days  = var.scientific_artifacts.retention_days
    region          = local.selected_target.region
    object_root     = local.scientific_artifacts_root
    object_roles    = local.scientific_artifacts_object_roles
    writer_paths    = sort([for tenant in values(local.scientific_artifact_tenants) : tenant.path])
    lifecycle_rules = [for rule in local.scientific_artifacts_lifecycle_rules : rule.id]
    credential_mode = "TENANT_ISOLATED_BROKER_KEYS"
    migration_phase = var.scientific_artifacts.migration.phase
  }

  lifecycle {
    precondition {
      condition = length(distinct([
        for tenant_id in var.scientific_artifacts.tenant_ids : substr(sha256(tenant_id), 0, 32)
      ])) == length(var.scientific_artifacts.tenant_ids)
      error_message = "Scientific artifact tenant IDs collide in the provider/Kubernetes resource-name hash namespace; no tenant identity may be provisioned or routed until every 32-hex resource suffix is unique."
    }
    precondition {
      condition     = var.scientific_artifacts.object_storage.bucket_name != var.reference_data.object_storage.bucket_name
      error_message = "the scientific result store must be a distinct bucket from the reference-data plane; results and immutable public inputs never share retention, policy or a writer key."
    }
    precondition {
      condition = (
        local.scientific_artifacts_retain ==
        (var.scientific_artifacts.lifecycle.retention_mode == "retain")
      )
      error_message = "scientific artifact retention mode must resolve to exactly one of the retained or disposable bucket resources."
    }
  }

  depends_on = [terraform_data.target_contract]
}

resource "nebius_iam_v1_service_account" "scientific_artifact_tenant" {
  for_each = local.scientific_artifact_tenants

  parent_id   = var.project_id
  name        = each.value.name
  description = "Isolated object-store principal for one scientific artifact tenant prefix"
  labels = merge(local.common_labels, {
    purpose     = "scientific-artifact-tenant-broker"
    tenant_hash = substr(sha256(each.key), 0, 32)
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v1_group" "scientific_artifact_tenant" {
  for_each = local.scientific_artifact_tenants

  parent_id = var.project_id
  name      = "${each.value.name}-writers"
  labels = merge(local.common_labels, {
    purpose     = "scientific-artifact-tenant-prefix"
    tenant_hash = substr(sha256(each.key), 0, 32)
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v1_group_membership" "scientific_artifact_tenant" {
  for_each = local.scientific_artifact_tenants

  parent_id = nebius_iam_v1_group.scientific_artifact_tenant[each.key].id
  member_id = nebius_iam_v1_service_account.scientific_artifact_tenant[each.key].id

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v2_access_key" "scientific_artifact_tenant" {
  for_each = local.scientific_artifact_tenants

  parent_id           = var.project_id
  name                = "${each.value.name}-broker"
  description         = "Credential mounted only by the isolated broker for one tenant prefix"
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose     = "scientific-artifact-tenant-broker"
    tenant_hash = substr(sha256(each.key), 0, 32)
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.scientific_artifact_tenant[each.key].id
    }
  }

  depends_on = [nebius_iam_v1_group_membership.scientific_artifact_tenant]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v1_service_account" "scientific_artifact_tenant_generation" {
  for_each = local.scientific_artifact_additional_generations

  parent_id   = var.project_id
  name        = each.value.name
  description = "Additive generation ${each.value.generation} object-store principal for one scientific artifact tenant prefix"
  labels = merge(local.common_labels, {
    purpose              = "scientific-artifact-tenant-broker"
    tenant_hash          = substr(sha256(each.value.tenant_id), 0, 32)
    credential_generation = tostring(each.value.generation)
  })

  lifecycle { prevent_destroy = true }
}

resource "nebius_iam_v1_group" "scientific_artifact_tenant_generation" {
  for_each = local.scientific_artifact_additional_generations

  parent_id = var.project_id
  name      = "${each.value.name}-writers"
  labels = merge(local.common_labels, {
    purpose              = "scientific-artifact-tenant-prefix"
    tenant_hash          = substr(sha256(each.value.tenant_id), 0, 32)
    credential_generation = tostring(each.value.generation)
  })

  lifecycle { prevent_destroy = true }
}

resource "nebius_iam_v1_group_membership" "scientific_artifact_tenant_generation" {
  for_each = local.scientific_artifact_additional_generations

  parent_id = nebius_iam_v1_group.scientific_artifact_tenant_generation[each.key].id
  member_id = nebius_iam_v1_service_account.scientific_artifact_tenant_generation[each.key].id

  lifecycle { prevent_destroy = true }
}

resource "nebius_iam_v2_access_key" "scientific_artifact_tenant_generation" {
  for_each = local.scientific_artifact_additional_generations

  parent_id            = var.project_id
  name                 = "${each.value.name}-broker"
  description          = "Additive generation ${each.value.generation} credential mounted only by one tenant broker"
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose              = "scientific-artifact-tenant-broker"
    tenant_hash          = substr(sha256(each.value.tenant_id), 0, 32)
    credential_generation = tostring(each.value.generation)
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.scientific_artifact_tenant_generation[each.key].id
    }
  }

  depends_on = [nebius_iam_v1_group_membership.scientific_artifact_tenant_generation]

  lifecycle { prevent_destroy = true }
}

# State-preserving transition for the pre-SAI-19 shared writer identity. These
# four resource addresses already exist in retained state and therefore remain
# declared: removing them would plan destructive cloud deletes. The first
# legacy-overlap apply retains its existing scientific/v1/* grant so the
# infrastructure-first orchestrator cannot strand running gateway replicas.
# This candidate has no path that omits that rule: bare receipt digests cannot
# authorize a one-way transition. A separately reviewed successor must add
# cryptographic multi-observer verification before deauthorization. The
# service account, group and key remain declared and protected; this static
# remediation never revokes or deletes them.
resource "nebius_iam_v1_service_account" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-scientific-artifacts"
  description = local.scientific_artifacts_legacy_authorized ? "Legacy scientific artifact writer retained for additive migration overlap" : "Quarantined legacy scientific artifact writer retained without bucket authorization"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-writer-quarantined"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })

  depends_on = [terraform_data.scientific_artifacts_contract]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v1_group" "scientific_artifacts_writers" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = var.project_id
  name      = "${local.resource_name}-scientific-artifact-writers"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-write-quarantined"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })

  depends_on = [terraform_data.scientific_artifacts_contract]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_iam_v1_group_membership" "scientific_artifacts_writer" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
  member_id = nebius_iam_v1_service_account.scientific_artifacts[0].id

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_storage_v1_bucket" "scientific_artifacts" {
  count = local.scientific_artifacts_retain ? 1 : 0

  parent_id             = var.project_id
  name                  = var.scientific_artifacts.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.scientific_artifacts.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "durable"
  })
  bucket_policy = {
    rules = concat(
      [for binding in values(local.scientific_artifact_authorized_generations) : {
        group_id = binding.generation == 1 ? (
          nebius_iam_v1_group.scientific_artifact_tenant[binding.tenant_id].id
        ) : nebius_iam_v1_group.scientific_artifact_tenant_generation["${binding.tenant_id}:${binding.generation}"].id
        paths    = [binding.path]
        roles    = local.scientific_artifacts_object_roles
      }],
      local.scientific_artifacts_legacy_authorized ? [{
        group_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
        paths    = ["${local.scientific_artifacts_root}/*"]
        roles    = local.scientific_artifacts_object_roles
      }] : [],
    )
  }
  lifecycle_configuration = {
    rules = local.scientific_artifacts_lifecycle_rules
  }

  depends_on = [
    nebius_iam_v1_group_membership.scientific_artifact_tenant,
    nebius_iam_v1_group_membership.scientific_artifact_tenant_generation,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_storage_v1_bucket" "scientific_artifacts_disposable" {
  count = local.scientific_artifacts_dispose ? 1 : 0

  parent_id             = var.project_id
  name                  = var.scientific_artifacts.object_storage.bucket_name
  versioning_policy     = "ENABLED"
  max_size_bytes        = var.scientific_artifacts.object_storage.max_size_gib * 1024 * 1024 * 1024
  default_storage_class = "STANDARD"
  force_storage_class   = true
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-results"
    retention = "disposable-empty-only"
  })
  bucket_policy = {
    rules = concat(
      [for binding in values(local.scientific_artifact_authorized_generations) : {
        group_id = binding.generation == 1 ? (
          nebius_iam_v1_group.scientific_artifact_tenant[binding.tenant_id].id
        ) : nebius_iam_v1_group.scientific_artifact_tenant_generation["${binding.tenant_id}:${binding.generation}"].id
        paths    = [binding.path]
        roles    = local.scientific_artifacts_object_roles
      }],
      local.scientific_artifacts_legacy_authorized ? [{
        group_id = nebius_iam_v1_group.scientific_artifacts_writers[0].id
        paths    = ["${local.scientific_artifacts_root}/*"]
        roles    = local.scientific_artifacts_object_roles
      }] : [],
    )
  }
  lifecycle_configuration = {
    rules = local.scientific_artifacts_lifecycle_rules
  }

  depends_on = [
    nebius_iam_v1_group_membership.scientific_artifact_tenant,
    nebius_iam_v1_group_membership.scientific_artifact_tenant_generation,
  ]
}

resource "nebius_iam_v2_access_key" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  parent_id   = var.project_id
  name        = "${local.resource_name}-scientific-artifacts"
  description = local.scientific_artifacts_legacy_authorized ? "Legacy key retained for additive migration overlap" : "Quarantined legacy key retained without any artifact bucket policy authorization"
  secret_delivery_mode = "MYSTERY_BOX"
  labels = merge(local.common_labels, {
    purpose   = "scientific-artifact-object-write-quarantined"
    retention = local.scientific_artifacts_retain ? "durable" : "ephemeral"
  })
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.scientific_artifacts[0].id
    }
  }

  depends_on = [
    nebius_storage_v1_bucket.scientific_artifacts,
    nebius_storage_v1_bucket.scientific_artifacts_disposable,
  ]

  lifecycle {
    prevent_destroy = true
  }
}
