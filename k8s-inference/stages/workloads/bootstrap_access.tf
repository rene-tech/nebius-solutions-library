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
      enabled        = true
      secretName     = kubernetes_secret_v1.bootstrap_access.metadata[0].name
      tokenKey       = "token"
      principalId    = local.bootstrap_access_principal
      tenantId       = local.bootstrap_access_tenant_id
      name           = "Terraform bootstrap MCP and inference"
      scopes         = local.bootstrap_access_scopes
      models         = local.bootstrap_access_models
      maxConcurrency = 32
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
      enabled        = local.scientific_access_enabled
      secretName     = local.scientific_access_secret_name
      tokenKey       = "token"
      principalId    = local.scientific_access_principal
      tenantId       = local.scientific_access_tenant_id
      name           = "Terraform academic scientific access"
      scopes         = local.scientific_access_scopes
      models         = local.scientific_access_models
      maxConcurrency = 32
    }
  }
}

resource "random_id" "bootstrap_access_token_id" {
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.bootstrap_access_tenant_id
  }
}

resource "random_password" "bootstrap_access_token_secret" {
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.bootstrap_access_tenant_id
  }
}

resource "random_id" "scientific_access_token_id" {
  count       = local.scientific_access_enabled ? 1 : 0
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.scientific_access_tenant_id
  }
}

resource "random_password" "scientific_access_token_secret" {
  count   = local.scientific_access_enabled ? 1 : 0
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.scientific_access_tenant_id
  }
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

  depends_on = [terraform_data.cluster_contract]
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

  depends_on = [terraform_data.cluster_contract]
}
