locals {
  active_payload_keyring_name = var.keyring_generations.payload.active == 1 ? kubernetes_secret_v1.payload_keyring.metadata[0].name : kubernetes_secret_v1.payload_keyring_versioned[tostring(var.keyring_generations.payload.active)].metadata[0].name
  active_ledger_keyring_name  = var.keyring_generations.ledger.active == 1 ? kubernetes_secret_v1.ledger_keyring.metadata[0].name : kubernetes_secret_v1.ledger_keyring_versioned[tostring(var.keyring_generations.ledger.active)].metadata[0].name
  active_token_pepper_name    = var.keyring_generations.pepper.active == 1 ? kubernetes_secret_v1.token_pepper.metadata[0].name : kubernetes_secret_v1.token_pepper_versioned[tostring(var.keyring_generations.pepper.active)].metadata[0].name
  active_route_attestors_name = var.keyring_generations.attestor.active == 1 ? kubernetes_secret_v1.route_attestors.metadata[0].name : kubernetes_secret_v1.route_attestors_versioned[tostring(var.keyring_generations.attestor.active)].metadata[0].name
  active_admin_secret_name    = var.credential_generations.admin == 1 ? kubernetes_secret_v1.admin.metadata[0].name : kubernetes_secret_v1.admin_versioned[tostring(var.credential_generations.admin)].metadata[0].name

  database_accounts = {
    owner = {
      username = "fs2serve"
    }
    runtime = {
      username = "fs2_serve_runtime_login"
    }
    maintenance = {
      username = "fs2_serve_maintenance_login"
    }
    activation = {
      username = "fs2_serve_activation_login"
    }
    restore_verifier = {
      username = "fs2_serve_restore_verifier_login"
    }
    reporting = {
      username = "fs2_serve_reporting_login"
    }
    monitoring = {
      username = "fs2_serve_monitoring_login"
    }
  }

  consumer_database_secrets = {
    migrations = {
      namespace   = "fs2-system"
      secret_name = "fs2-serve-database-migrations"
      account     = "owner"
    }
    runtime = {
      namespace   = "fs2-system"
      secret_name = "fs2-serve-database"
      account     = "runtime"
    }
    maintenance = {
      namespace   = "fs2-system"
      secret_name = "fs2-serve-database-maintenance"
      account     = "maintenance"
    }
    activation = {
      namespace   = "fs2-system"
      secret_name = "fs2-serve-database-activation"
      account     = "activation"
    }
    restore_verifier = {
      namespace   = "fs2-system"
      secret_name = "fs2-serve-database-restore-verifier"
      account     = "restore_verifier"
    }
    reporting = {
      namespace   = "fs2-observability"
      secret_name = "fs2-serve-database-reporting"
      account     = "reporting"
    }
    monitoring = {
      namespace = "fs2-data"
      # CNPG owns fs2-control-db-monitoring as the login's basic-auth Secret.
      # Keep the URL/CA consumer interface at a distinct Terraform-owned name.
      secret_name = "fs2-serve-database-monitoring"
      account     = "monitoring"
    }
  }
}

resource "random_password" "database" {
  for_each = local.database_accounts

  length  = 40
  special = false
}

resource "random_password" "key_material" {
  for_each = toset(["payload", "ledger", "pepper", "attestor"])

  length  = 32
  special = false
}

resource "random_password" "admin_token" {
  length  = 48
  special = false
}

resource "kubernetes_secret_v1" "database_account" {
  for_each = local.database_accounts

  metadata {
    name      = each.key == "owner" ? "fs2-control-db-owner" : "fs2-control-db-${replace(each.key, "_", "-")}"
    namespace = "fs2-data"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-purpose" = each.key })
  }

  type = "kubernetes.io/basic-auth"
  # Generation 1 is persisted deliberately until the encrypted-state migration
  # has imported these exact values. Replacing this address would invalidate
  # existing database clients.
  data = {
    username = each.value.username
    password = random_password.database[each.key].result
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "payload_keyring" {
  metadata {
    name      = "fs2-serve-payload-keyring"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    "keyring.json" = jsonencode({ active_key_id = "payload-v1", keys = { "payload-v1" = base64encode(random_password.key_material["payload"].result) } })
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "ledger_keyring" {
  metadata {
    name      = "fs2-serve-ledger-hmac-keyring"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    "keyring.json" = jsonencode({ active_key_id = "ledger-v1", keys = { "ledger-v1" = base64encode(random_password.key_material["ledger"].result) } })
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "token_pepper" {
  metadata {
    name      = "fs2-serve-token-pepper"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    "keyring.json" = jsonencode({ active_key_id = "pepper-v1", keys = { "pepper-v1" = base64encode(random_password.key_material["pepper"].result) } })
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "route_attestors" {
  metadata {
    name      = "fs2-serve-route-attestors"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    "attestors.json" = jsonencode({
      "sha256:${sha256(random_password.key_material["attestor"].result)}" = trimsuffix(replace(replace(base64encode(random_password.key_material["attestor"].result), "+", "-"), "/", "_"), "=")
    })
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "admin" {
  metadata {
    name      = "fs2-serve-admin"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    token = random_password.admin_token.result
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "admin_versioned" {
  for_each = toset([for generation in var.credential_generation_history.admin : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-admin-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-generation" = each.key })
  }
  type             = "Opaque"
  data_wo          = { token = lookup(var.admin_tokens, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition     = try(length(var.admin_tokens[each.key]) >= 32, false)
      error_message = "Every retained admin generation greater than 1 requires an externally escrowed token of at least 32 characters."
    }
    prevent_destroy = true
  }
}

# Later generations never overwrite the fixed generation-1 Secret names. The
# supplied documents contain the active key plus every retained predecessor;
# data_wo keeps their values out of new plan and state payloads.
resource "kubernetes_secret_v1" "payload_keyring_versioned" {
  for_each = toset([for generation in var.keyring_generations.payload.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-payload-keyring-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
  }
  type             = "Opaque"
  data_wo          = { "keyring.json" = lookup(var.payload_keyrings_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.payload_keyrings_json, each.key, null) != null &&
        try(jsondecode(var.payload_keyrings_json[each.key]).active_key_id, "") == "payload-v${each.key}" &&
        try(jsondecode(var.payload_keyrings_json[each.key]).keys["payload-v1"], "") == base64encode(random_password.key_material["payload"].result) &&
        try(toset(keys(jsondecode(var.payload_keyrings_json[each.key]).keys)), toset([])) == toset([
          for generation in range(1, tonumber(each.key) + 1) : "payload-v${generation}"
        ])
      )
      error_message = "Each payload keyring document must retain the exact payload-v1 value and every generation through its immutable keyring ID."
    }
    prevent_destroy = true
  }
}

resource "kubernetes_secret_v1" "ledger_keyring_versioned" {
  for_each = toset([for generation in var.keyring_generations.ledger.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-ledger-hmac-keyring-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
  }
  type             = "Opaque"
  data_wo          = { "keyring.json" = lookup(var.ledger_keyrings_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.ledger_keyrings_json, each.key, null) != null &&
        try(jsondecode(var.ledger_keyrings_json[each.key]).active_key_id, "") == "ledger-v${each.key}" &&
        try(jsondecode(var.ledger_keyrings_json[each.key]).keys["ledger-v1"], "") == base64encode(random_password.key_material["ledger"].result) &&
        try(toset(keys(jsondecode(var.ledger_keyrings_json[each.key]).keys)), toset([])) == toset([
          for generation in range(1, tonumber(each.key) + 1) : "ledger-v${generation}"
        ])
      )
      error_message = "Each ledger keyring document must retain the exact ledger-v1 value and every generation through its immutable keyring ID."
    }
    prevent_destroy = true
  }
}

resource "kubernetes_secret_v1" "token_pepper_versioned" {
  for_each = toset([for generation in var.keyring_generations.pepper.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-token-pepper-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
  }
  type             = "Opaque"
  data_wo          = { "keyring.json" = lookup(var.token_pepper_keyrings_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.token_pepper_keyrings_json, each.key, null) != null &&
        try(jsondecode(var.token_pepper_keyrings_json[each.key]).active_key_id, "") == "pepper-v${each.key}" &&
        try(jsondecode(var.token_pepper_keyrings_json[each.key]).keys["pepper-v1"], "") == base64encode(random_password.key_material["pepper"].result) &&
        try(toset(keys(jsondecode(var.token_pepper_keyrings_json[each.key]).keys)), toset([])) == toset([
          for generation in range(1, tonumber(each.key) + 1) : "pepper-v${generation}"
        ])
      )
      error_message = "Each pepper keyring document must retain the exact pepper-v1 value and every generation through its immutable keyring ID."
    }
    prevent_destroy = true
  }
}

resource "kubernetes_secret_v1" "route_attestors_versioned" {
  for_each = toset([for generation in var.keyring_generations.attestor.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-route-attestors-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
  }
  type             = "Opaque"
  data_wo          = { "attestors.json" = lookup(var.route_attestors_sets_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.route_attestors_sets_json, each.key, null) != null &&
        length(try(jsondecode(var.route_attestors_sets_json[each.key]), {})) == tonumber(each.key) &&
        try(jsondecode(var.route_attestors_sets_json[each.key])["sha256:${sha256(random_password.key_material["attestor"].result)}"], "") == trimsuffix(replace(replace(base64encode(random_password.key_material["attestor"].result), "+", "-"), "/", "_"), "=")
      )
      error_message = "Each attestor document must retain the exact generation-1 public key and one entry per immutable attestor generation."
    }
    prevent_destroy = true
  }
}

resource "kubernetes_secret_v1" "ngc_api_key" {
  count = local.ngc_api_key_required ? 1 : 0

  metadata {
    name      = "ngc-api-key"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  type = "Opaque"
  data_wo = {
    NGC_API_KEY = var.ngc_api_key
  }
  data_wo_revision = var.credential_generations.registry
  depends_on       = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "nvcrio_cred" {
  count = local.model_nvcr_credentials_required ? 1 : 0

  metadata {
    name      = "nvcrio-cred"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  type = "kubernetes.io/dockerconfigjson"
  data_wo = {
    ".dockerconfigjson" = var.nvcrio_dockerconfigjson
  }
  data_wo_revision = var.credential_generations.registry
  depends_on       = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "dcgm_exporter_nvcrio" {
  count = local.dcgm_nvcr_credentials_required ? 1 : 0

  metadata {
    name      = "fs2-dcgm-exporter-nvcrio"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }
  type = "kubernetes.io/dockerconfigjson"
  data_wo = {
    ".dockerconfigjson" = var.nvcrio_dockerconfigjson
  }
  data_wo_revision = var.credential_generations.registry
  depends_on       = [terraform_data.cluster_contract]
}
