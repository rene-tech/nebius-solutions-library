locals {
  bootstrap_access_secret_name = "fs2-serve-bootstrap-access"
  bootstrap_access_principal   = "terraform-bootstrap-client"
  bootstrap_access_tenant_id   = local.selected_target.tenant_id
  # The bootstrap client follows the live catalog so a dynamically added model
  # does not require rotating this Terraform-owned credential. Protocol and
  # tenant scopes remain bounded; catalog/routing policy is still authoritative.
  bootstrap_access_models = ["*"]
  bootstrap_access_scopes = [
    "catalog.read",
    "inference.invoke",
    "mcp.invoke",
    "operations.read",
    "operations.result",
    "operations.cancel",
    "operations.acknowledge",
    "use.nonclinical",
    "use.noncommercial",
  ]
  bootstrap_access_token = sensitive(
    "fs2_pat_${random_id.bootstrap_access_token_id.hex}_${random_password.bootstrap_access_token_secret.result}"
  )
  bootstrap_access_overrides = {
    bootstrapAccess = {
      enabled = true
      secretName = var.credential_generations.access == 1 ? (
        kubernetes_secret_v1.bootstrap_access.metadata[0].name
      ) : kubernetes_secret_v1.bootstrap_access_versioned[tostring(var.credential_generations.access)].metadata[0].name
      tokenKey       = "token"
      principalId    = local.bootstrap_access_principal
      tenantId       = local.bootstrap_access_tenant_id
      name           = "Terraform bootstrap MCP and inference"
      scopes         = local.bootstrap_access_scopes
      models         = local.bootstrap_access_models
      maxConcurrency = 32
      expiresAt      = var.bootstrap_access_expires_at
    }
  }

  # Academic scientific execution is a separate tenant boundary. Keep the
  # general MCP/inference credential on the cluster tenant and mint a distinct
  # PAT only when the private academic-asset plane is enabled.
  scientific_access_enabled     = var.academic_assets.enabled
  scientific_access_secret_name = "fs2-serve-scientific-access"
  scientific_access_principal   = "terraform-academic-scientific-client"
  scientific_access_tenant_id   = var.academic_assets.tenant_id
  scientific_access_models      = ["*"]
  scientific_access_scopes      = local.bootstrap_access_scopes
  scientific_access_token = local.scientific_access_enabled ? sensitive(
    "fs2_pat_${random_id.scientific_access_token_id[0].hex}_${random_password.scientific_access_token_secret[0].result}"
  ) : null
  scientific_access_overrides = {
    scientificAccess = {
      enabled = local.scientific_access_enabled
      secretName = var.credential_generations.access == 1 ? (
        local.scientific_access_secret_name
      ) : kubernetes_secret_v1.scientific_access_versioned[tostring(var.credential_generations.access)].metadata[0].name
      tokenKey       = "token"
      principalId    = local.scientific_access_principal
      tenantId       = local.scientific_access_tenant_id
      name           = "Terraform academic scientific access"
      scopes         = local.scientific_access_scopes
      models         = local.scientific_access_models
      maxConcurrency = 32
      expiresAt      = var.bootstrap_access_expires_at
    }
  }

  # The public website reads the same tenant-filtered catalog but must never
  # inherit the academic client's invoke or operation privileges. The distinct
  # token also permits independent rotation without disrupting customers.
  website_access_enabled     = var.academic_assets.enabled
  website_access_secret_name = "fs2-serve-website-access"
  website_access_principal   = "terraform-scientific-ai-website"
  website_access_tenant_id   = var.academic_assets.tenant_id
  website_access_models      = ["*"]
  website_access_scopes      = ["catalog.read"]
  website_access_token = local.website_access_enabled ? sensitive(
    "fs2_pat_${random_id.website_access_token_id[0].hex}_${random_password.website_access_token_secret[0].result}"
  ) : null
  website_access_overrides = {
    websiteAccess = {
      enabled = local.website_access_enabled
      secretName = var.credential_generations.access == 1 ? (
        local.website_access_secret_name
      ) : kubernetes_secret_v1.website_access_versioned[tostring(var.credential_generations.access)].metadata[0].name
      tokenKey       = "token"
      principalId    = local.website_access_principal
      tenantId       = local.website_access_tenant_id
      name           = "Terraform Scientific AI website catalog"
      scopes         = local.website_access_scopes
      models         = local.website_access_models
      maxConcurrency = 1
      expiresAt      = var.bootstrap_access_expires_at
    }
  }

  # A rotation is independently revocable only when every generation and
  # audience has a different PAT ID. The v1 IDs remain fixed and protected;
  # later generations are append-only external inputs.
  bootstrap_access_versioned_pat_ids = [
    for generation in var.credential_generation_history.access :
    try(regex("^fs2_pat_([0-9a-f]{32})_[A-Za-z0-9_-]{32,}$", var.bootstrap_access_tokens[tostring(generation)])[0], "")
    if generation > 1
  ]
  scientific_access_versioned_pat_ids = local.scientific_access_enabled ? [
    for generation in var.credential_generation_history.access :
    try(regex("^fs2_pat_([0-9a-f]{32})_[A-Za-z0-9_-]{32,}$", var.scientific_access_tokens[tostring(generation)])[0], "")
    if generation > 1
  ] : []
  website_access_versioned_pat_ids = local.website_access_enabled ? [
    for generation in var.credential_generation_history.access :
    try(regex("^fs2_pat_([0-9a-f]{32})_[A-Za-z0-9_-]{32,}$", var.website_access_tokens[tostring(generation)])[0], "")
    if generation > 1
  ] : []
  all_versioned_access_pat_ids = concat(
    local.bootstrap_access_versioned_pat_ids,
    local.scientific_access_versioned_pat_ids,
    local.website_access_versioned_pat_ids,
  )
  generation_one_access_pat_ids = concat(
    [random_id.bootstrap_access_token_id.hex],
    local.scientific_access_enabled ? [random_id.scientific_access_token_id[0].hex] : [],
    local.website_access_enabled ? [random_id.website_access_token_id[0].hex] : [],
  )
}

resource "random_id" "bootstrap_access_token_id" {
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.bootstrap_access_tenant_id
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_password" "bootstrap_access_token_secret" {
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.bootstrap_access_tenant_id
  }


  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_id" "scientific_access_token_id" {
  count       = local.scientific_access_enabled ? 1 : 0
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.scientific_access_tenant_id
  }


  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_password" "scientific_access_token_secret" {
  count   = local.scientific_access_enabled ? 1 : 0
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.scientific_access_tenant_id
  }


  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_id" "website_access_token_id" {
  count       = local.website_access_enabled ? 1 : 0
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.website_access_tenant_id
    principal  = local.website_access_principal
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_password" "website_access_token_secret" {
  count   = local.website_access_enabled ? 1 : 0
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.website_access_tenant_id
    principal  = local.website_access_principal
  }


  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "bootstrap_access" {
  metadata {
    name      = local.bootstrap_access_secret_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "bootstrap-mcp-inference"
    })
  }

  type = "Opaque"
  data = {
    token = local.bootstrap_access_token
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "bootstrap_access_versioned" {
  for_each = toset([for generation in var.credential_generation_history.access : tostring(generation) if generation > 1])

  metadata {
    name      = "${local.bootstrap_access_secret_name}-v${each.key}"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose"    = "bootstrap-mcp-inference"
      "fs2.nebius.ai/credential-generation" = each.key
    })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "pat-bootstrap"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        token = var.bootstrap_access_tokens[each.key]
      }))
    }
  }

  immutable        = true
  type             = "Opaque"
  data_wo          = { token = lookup(var.bootstrap_access_tokens, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        try(can(regex("^fs2_pat_[0-9a-f]{32}_[A-Za-z0-9_-]{32,}$", var.bootstrap_access_tokens[each.key])), false) &&
        length(local.all_versioned_access_pat_ids) == length(toset(local.all_versioned_access_pat_ids)) &&
        length(local.generation_one_access_pat_ids) == length(toset(local.generation_one_access_pat_ids)) &&
        length(setintersection(toset(local.all_versioned_access_pat_ids), toset(local.generation_one_access_pat_ids))) == 0
      )
      error_message = "Every retained access generation and audience requires a unique externally escrowed PAT ID that differs from both immutable generation-1 IDs."
    }
    prevent_destroy = true
  }


  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "scientific_access" {
  count = local.scientific_access_enabled ? 1 : 0

  metadata {
    name      = local.scientific_access_secret_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "academic-scientific-access"
    })
  }

  type = "Opaque"
  data = {
    token = local.scientific_access_token
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "scientific_access_versioned" {
  for_each = local.scientific_access_enabled ? toset([
    for generation in var.credential_generation_history.access : tostring(generation) if generation > 1
  ]) : toset([])

  metadata {
    name      = "${local.scientific_access_secret_name}-v${each.key}"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose"    = "academic-scientific-access"
      "fs2.nebius.ai/credential-generation" = each.key
    })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "pat-scientific"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        token = var.scientific_access_tokens[each.key]
      }))
    }
  }

  immutable        = true
  type             = "Opaque"
  data_wo          = { token = lookup(var.scientific_access_tokens, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        try(can(regex("^fs2_pat_[0-9a-f]{32}_[A-Za-z0-9_-]{32,}$", var.scientific_access_tokens[each.key])), false) &&
        length(local.all_versioned_access_pat_ids) == length(toset(local.all_versioned_access_pat_ids)) &&
        length(local.generation_one_access_pat_ids) == length(toset(local.generation_one_access_pat_ids)) &&
        length(setintersection(toset(local.all_versioned_access_pat_ids), toset(local.generation_one_access_pat_ids))) == 0
      )
      error_message = "Every retained access generation and audience requires a unique externally escrowed PAT ID that differs from both immutable generation-1 IDs."
    }
    prevent_destroy = true
  }


  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "website_access" {
  count = local.website_access_enabled ? 1 : 0

  metadata {
    name      = local.website_access_secret_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "scientific-ai-website-catalog"
    })
  }

  type = "Opaque"
  data = {
    token = local.website_access_token
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "website_access_versioned" {
  for_each = local.website_access_enabled ? toset([
    for generation in var.credential_generation_history.access : tostring(generation) if generation > 1
  ]) : toset([])

  metadata {
    name      = "${local.website_access_secret_name}-v${each.key}"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose"    = "scientific-ai-website-catalog"
      "fs2.nebius.ai/credential-generation" = each.key
    })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "pat-website"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        token = var.website_access_tokens[each.key]
      }))
    }
  }

  immutable        = true
  type             = "Opaque"
  data_wo          = { token = lookup(var.website_access_tokens, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        try(can(regex("^fs2_pat_[0-9a-f]{32}_[A-Za-z0-9_-]{32,}$", var.website_access_tokens[each.key])), false) &&
        length(local.all_versioned_access_pat_ids) == length(toset(local.all_versioned_access_pat_ids)) &&
        length(local.generation_one_access_pat_ids) == length(toset(local.generation_one_access_pat_ids)) &&
        length(setintersection(toset(local.all_versioned_access_pat_ids), toset(local.generation_one_access_pat_ids))) == 0
      )
      error_message = "Every retained access generation and audience requires a distinct PAT ID across bootstrap, scientific and website lineages."
    }
    prevent_destroy = true
  }

  depends_on = [terraform_data.credential_migration_gate]
}
