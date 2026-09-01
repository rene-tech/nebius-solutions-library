locals {
  bootstrap_access_secret_name = "fs2-serve-bootstrap-access"
  bootstrap_access_principal   = "terraform-bootstrap-client"
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
      tenantId       = local.selected_target.tenant_id
      name           = "Terraform bootstrap MCP and inference"
      scopes         = local.bootstrap_access_scopes
      models         = local.bootstrap_access_models
      maxConcurrency = 32
    }
  }
}

resource "random_id" "bootstrap_access_token_id" {
  byte_length = 16
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.selected_target.tenant_id
  }
}

resource "random_password" "bootstrap_access_token_secret" {
  length  = 48
  special = false
  keepers = {
    cluster_id = var.cluster_id
    tenant_id  = local.selected_target.tenant_id
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
