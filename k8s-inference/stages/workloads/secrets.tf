locals {
  active_payload_keyring_name      = var.keyring_generations.payload.active == 1 ? kubernetes_secret_v1.payload_keyring.metadata[0].name : kubernetes_secret_v1.payload_keyring_versioned[tostring(var.keyring_generations.payload.active)].metadata[0].name
  active_ledger_keyring_name       = var.keyring_generations.ledger.active == 1 ? kubernetes_secret_v1.ledger_keyring.metadata[0].name : kubernetes_secret_v1.ledger_keyring_versioned[tostring(var.keyring_generations.ledger.active)].metadata[0].name
  active_token_pepper_name         = var.keyring_generations.pepper.active == 1 ? kubernetes_secret_v1.token_pepper.metadata[0].name : kubernetes_secret_v1.token_pepper_versioned[tostring(var.keyring_generations.pepper.active)].metadata[0].name
  active_route_attestors_name      = var.keyring_generations.attestor.active == 1 ? kubernetes_secret_v1.route_attestors.metadata[0].name : kubernetes_secret_v1.route_attestors_versioned[tostring(var.keyring_generations.attestor.active)].metadata[0].name
  active_storage_keyring_name      = var.keyring_generations.storage.active == 1 ? kubernetes_secret_v1.storage_keyring.metadata[0].name : kubernetes_secret_v1.storage_keyring_versioned[tostring(var.keyring_generations.storage.active)].metadata[0].name
  active_storage_name_keyring_name = var.keyring_generations.storage_name.active == 1 ? kubernetes_secret_v1.storage_keyring.metadata[0].name : kubernetes_secret_v1.storage_name_keyring_versioned[tostring(var.keyring_generations.storage_name.active)].metadata[0].name
  active_admin_secret_name         = var.credential_generations.admin == 1 ? kubernetes_secret_v1.admin.metadata[0].name : kubernetes_secret_v1.admin_versioned[tostring(var.credential_generations.admin)].metadata[0].name
  active_database_consumer_secret_names = {
    for consumer, definition in local.consumer_database_secrets : consumer => (
      var.credential_generations.database == 1 ?
      kubernetes_secret_v1.database_consumer[consumer].metadata[0].name :
      kubernetes_secret_v1.database_consumer_versioned["${var.credential_generations.database}:${consumer}"].metadata[0].name
    )
  }
  active_grafana_datasource_secret_name = var.credential_generations.database == 1 ? kubernetes_secret_v1.grafana_datasource.metadata[0].name : kubernetes_secret_v1.grafana_datasource_versioned[tostring(var.credential_generations.database)].metadata[0].name
  active_ngc_api_key_secret_name        = var.credential_generations.registry == 1 ? "ngc-api-key" : "ngc-api-key-v${var.credential_generations.registry}"
  active_nvcrio_secret_name             = var.credential_generations.registry == 1 ? "nvcrio-cred" : "nvcrio-cred-v${var.credential_generations.registry}"
  active_dcgm_nvcrio_secret_name        = var.credential_generations.registry == 1 ? "fs2-dcgm-exporter-nvcrio" : "fs2-dcgm-exporter-nvcrio-v${var.credential_generations.registry}"
  retained_nvcrio_secret_names = [
    for generation in sort(tolist(var.credential_generation_history.registry)) :
    generation == 1 ? "nvcrio-cred" : "nvcrio-cred-v${generation}"
  ]
  retained_dcgm_nvcrio_secret_names = [
    for generation in sort(tolist(var.credential_generation_history.registry)) :
    generation == 1 ? "fs2-dcgm-exporter-nvcrio" : "fs2-dcgm-exporter-nvcrio-v${generation}"
  ]

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

  database_versioned_accounts = {
    for pair in setproduct(
      setsubtract(var.credential_generation_history.database, toset([1])),
      toset(keys(local.database_accounts)),
      ) : "${pair[0]}:${pair[1]}" => {
      generation = pair[0]
      account    = pair[1]
      username   = "${local.database_accounts[pair[1]].username}_v${pair[0]}"
    }
  }
  database_versioned_consumers = {
    for pair in setproduct(
      setsubtract(var.credential_generation_history.database, toset([1])),
      toset(keys(local.consumer_database_secrets)),
      ) : "${pair[0]}:${pair[1]}" => {
      generation = pair[0]
      consumer   = pair[1]
      definition = local.consumer_database_secrets[pair[1]]
    }
  }
  active_database_usernames = {
    for account, definition in local.database_accounts : account => (
      var.credential_generations.database == 1 ?
      definition.username :
      "${definition.username}_v${var.credential_generations.database}"
    )
  }
  active_database_passwords = {
    for account in keys(local.database_accounts) : account => (
      var.credential_generations.database == 1 ?
      random_password.database[account].result :
      var.database_passwords[tostring(var.credential_generations.database)][account]
    )
  }
}

resource "random_password" "database" {
  for_each = local.database_accounts

  length  = 40
  special = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_password" "key_material" {
  for_each = toset(["payload", "ledger", "pepper", "attestor", "storage", "storage_name"])

  length  = 32
  special = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "random_password" "admin_token" {
  length  = 48
  special = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.credential_migration_gate]
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

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "database_account_versioned" {
  for_each = local.database_versioned_accounts

  metadata {
    name = each.value.account == "owner" ? (
      "fs2-control-db-owner-v${each.value.generation}"
      ) : (
      "fs2-control-db-${replace(each.value.account, "_", "-")}-v${each.value.generation}"
    )
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose"    = each.value.account
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "database-logins"
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        username = sha256(each.value.username)
        password = sha256(var.database_passwords[tostring(each.value.generation)][each.value.account])
      }))
    }
  }

  immutable = true
  type      = "kubernetes.io/basic-auth"
  data_wo = {
    username = each.value.username
    password = var.database_passwords[tostring(each.value.generation)][each.value.account]
  }
  data_wo_revision = each.value.generation

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ignore_changes  = all
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

# Generation 1 preserves the two independently generated storage keys at the
# deployed legacy Secret address. payload-v1 is retained only as a read key for
# customer-storage rows written before the dedicated cipher was introduced.
resource "kubernetes_secret_v1" "storage_keyring" {
  metadata {
    name      = "fs2-serve-storage-keyring"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  type = "Opaque"
  data_wo = {
    "keyring.json" = jsonencode({
      active_key_id = "storage-v1"
      keys = {
        "payload-v1" = base64encode(random_password.key_material["payload"].result)
        "storage-v1" = base64encode(random_password.key_material["storage"].result)
      }
    })
    "name-keyring.json" = jsonencode({
      active_key_id = "storage-name-v1"
      keys          = { "storage-name-v1" = base64encode(random_password.key_material["storage_name"].result) }
    })
  }
  data_wo_revision = 1
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ignore_changes  = all
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ignore_changes  = all
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ignore_changes  = all
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ignore_changes  = all
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "admin_versioned" {
  for_each = toset([for generation in var.credential_generation_history.admin : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-admin-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "admin-token"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        token = sha256(var.admin_tokens[each.key])
      }))
    }
  }
  immutable        = true
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

  depends_on = [terraform_data.credential_migration_gate]
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
    annotations = {
      "fs2.nebius.ai/credential-class"      = "payload-keyring"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "keyring.json" = sha256(var.payload_keyrings_json[each.key])
      }))
    }
  }
  immutable        = true
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

  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "ledger_keyring_versioned" {
  for_each = toset([for generation in var.keyring_generations.ledger.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-ledger-hmac-keyring-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "ledger-keyring"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "keyring.json" = sha256(var.ledger_keyrings_json[each.key])
      }))
    }
  }
  immutable        = true
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

  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "token_pepper_versioned" {
  for_each = toset([for generation in var.keyring_generations.pepper.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-token-pepper-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "pat-pepper-keyring"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "keyring.json" = sha256(var.token_pepper_keyrings_json[each.key])
      }))
    }
  }
  immutable        = true
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

  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "route_attestors_versioned" {
  for_each = toset([for generation in var.keyring_generations.attestor.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-route-attestors-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "route-attestors"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "attestors.json" = sha256(var.route_attestors_sets_json[each.key])
      }))
    }
  }
  immutable        = true
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

  depends_on = [terraform_data.credential_migration_gate]
}

# Cipher and deterministic-name material advance independently. New values use
# write-only arguments and immutable Secret names; every document retains its
# predecessors so an application rollback never loses a read generation.
resource "kubernetes_secret_v1" "storage_keyring_versioned" {
  for_each = toset([for generation in var.keyring_generations.storage.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-storage-keyring-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "customer-storage-cipher-keyring"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "keyring.json" = sha256(var.storage_keyrings_json[each.key])
      }))
    }
  }
  immutable        = true
  type             = "Opaque"
  data_wo          = { "keyring.json" = lookup(var.storage_keyrings_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.storage_keyrings_json, each.key, null) != null &&
        can(regex("^storage-v[1-9][0-9]*$", try(jsondecode(var.storage_keyrings_json[each.key]).active_key_id, ""))) &&
        contains(try(keys(jsondecode(var.storage_keyrings_json[each.key]).keys), []), try(jsondecode(var.storage_keyrings_json[each.key]).active_key_id, "")) &&
        try(jsondecode(var.storage_keyrings_json[each.key]).keys["payload-v1"], "") == base64encode(random_password.key_material["payload"].result) &&
        try(jsondecode(var.storage_keyrings_json[each.key]).keys["storage-v1"], "") == base64encode(random_password.key_material["storage"].result) &&
        alltrue([for key_id in try(keys(jsondecode(var.storage_keyrings_json[each.key]).keys), []) : key_id == "payload-v1" || can(regex("^storage-v[1-9][0-9]*$", key_id))]) &&
        (
          tonumber(each.key) == 2 ?
          length(setsubtract(toset(["payload-v1", "storage-v1"]), try(toset(keys(jsondecode(var.storage_keyrings_json[each.key]).keys)), toset([])))) == 0 :
          length(setsubtract(
            try(toset(keys(jsondecode(var.storage_keyrings_json[tostring(tonumber(each.key) - 1)]).keys)), toset(["__missing_predecessor__"])),
            try(toset(keys(jsondecode(var.storage_keyrings_json[each.key]).keys)), toset([]))
          )) == 0
        )
      )
      error_message = "Each immutable storage cipher bundle must preserve imported payload-v1/storage-v1 bytes, retain every predecessor key, select a retained storage key for writes, and use a new bundle generation for rollback."
    }
    prevent_destroy = true
  }

  depends_on = [terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "storage_name_keyring_versioned" {
  for_each = toset([for generation in var.keyring_generations.storage_name.retained : tostring(generation) if generation > 1])

  metadata {
    name      = "fs2-serve-storage-name-keyring-v${each.key}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/key-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "customer-storage-name-keyring"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        "name-keyring.json" = sha256(var.storage_name_keyrings_json[each.key])
      }))
    }
  }
  immutable        = true
  type             = "Opaque"
  data_wo          = { "name-keyring.json" = lookup(var.storage_name_keyrings_json, each.key, null) }
  data_wo_revision = tonumber(each.key)

  lifecycle {
    precondition {
      condition = (
        lookup(var.storage_name_keyrings_json, each.key, null) != null &&
        can(regex("^storage-name-v[1-9][0-9]*$", try(jsondecode(var.storage_name_keyrings_json[each.key]).active_key_id, ""))) &&
        contains(try(keys(jsondecode(var.storage_name_keyrings_json[each.key]).keys), []), try(jsondecode(var.storage_name_keyrings_json[each.key]).active_key_id, "")) &&
        try(jsondecode(var.storage_name_keyrings_json[each.key]).keys["storage-name-v1"], "") == base64encode(random_password.key_material["storage_name"].result) &&
        alltrue([for key_id in try(keys(jsondecode(var.storage_name_keyrings_json[each.key]).keys), []) : can(regex("^storage-name-v[1-9][0-9]*$", key_id))]) &&
        (
          tonumber(each.key) == 2 ?
          contains(try(keys(jsondecode(var.storage_name_keyrings_json[each.key]).keys), []), "storage-name-v1") :
          length(setsubtract(
            try(toset(keys(jsondecode(var.storage_name_keyrings_json[tostring(tonumber(each.key) - 1)]).keys)), toset(["__missing_predecessor__"])),
            try(toset(keys(jsondecode(var.storage_name_keyrings_json[each.key]).keys)), toset([]))
          )) == 0
        )
      )
      error_message = "Each immutable storage-name bundle must preserve storage-name-v1 bytes, retain every predecessor key, select a retained storage-name key, and use a new bundle generation for rollback."
    }
    prevent_destroy = true
  }

  depends_on = [terraform_data.credential_migration_gate]
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
    NGC_API_KEY = lookup(var.registry_ngc_api_keys, "1", null)
  }
  data_wo_revision = 1
  lifecycle {
    precondition {
      condition     = try(length(var.registry_ngc_api_keys["1"]) > 0, false)
      error_message = "Registry generation 1 must remain externally escrowed while its Secret is retained."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "ngc_api_key_versioned" {
  for_each = local.ngc_api_key_required ? toset([
    for generation in var.credential_generation_history.registry : tostring(generation) if generation > 1
  ]) : toset([])

  metadata {
    name      = "ngc-api-key-v${each.key}"
    namespace = "fs2-models"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "registry-credentials"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        NGC_API_KEY = sha256(var.registry_ngc_api_keys[each.key])
      }))
    }
  }
  immutable        = true
  type             = "Opaque"
  data_wo          = { NGC_API_KEY = lookup(var.registry_ngc_api_keys, each.key, null) }
  data_wo_revision = tonumber(each.key)
  lifecycle {
    precondition {
      condition     = try(length(var.registry_ngc_api_keys[each.key]) > 0, false)
      error_message = "Every retained NGC generation requires its externally escrowed API key."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ".dockerconfigjson" = lookup(var.registry_nvcrio_dockerconfigs, "1", null)
  }
  data_wo_revision = 1
  lifecycle {
    precondition {
      condition     = try(can(jsondecode(var.registry_nvcrio_dockerconfigs["1"])), false)
      error_message = "Registry generation 1 must remain externally escrowed while its pull Secret is retained."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "nvcrio_cred_versioned" {
  for_each = local.model_nvcr_credentials_required ? toset([
    for generation in var.credential_generation_history.registry : tostring(generation) if generation > 1
  ]) : toset([])

  metadata {
    name      = "nvcrio-cred-v${each.key}"
    namespace = "fs2-models"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "registry-credentials"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        ".dockerconfigjson" = sha256(var.registry_nvcrio_dockerconfigs[each.key])
      }))
    }
  }
  immutable        = true
  type             = "kubernetes.io/dockerconfigjson"
  data_wo          = { ".dockerconfigjson" = lookup(var.registry_nvcrio_dockerconfigs, each.key, null) }
  data_wo_revision = tonumber(each.key)
  lifecycle {
    precondition {
      condition     = try(can(jsondecode(var.registry_nvcrio_dockerconfigs[each.key])), false)
      error_message = "Every retained NVCR generation requires its externally escrowed Docker configuration."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
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
    ".dockerconfigjson" = lookup(var.registry_nvcrio_dockerconfigs, "1", null)
  }
  data_wo_revision = 1
  lifecycle {
    precondition {
      condition     = try(can(jsondecode(var.registry_nvcrio_dockerconfigs["1"])), false)
      error_message = "DCGM registry generation 1 must remain externally escrowed while retained."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "dcgm_exporter_nvcrio_versioned" {
  for_each = local.dcgm_nvcr_credentials_required ? toset([
    for generation in var.credential_generation_history.registry : tostring(generation) if generation > 1
  ]) : toset([])

  metadata {
    name      = "fs2-dcgm-exporter-nvcrio-v${each.key}"
    namespace = "fs2-observability"
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-generation" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-class"      = "registry-credentials"
      "fs2.nebius.ai/credential-generation" = each.key
      "fs2.nebius.ai/content-sha256" = sha256(jsonencode({
        ".dockerconfigjson" = sha256(var.registry_nvcrio_dockerconfigs[each.key])
      }))
    }
  }
  immutable        = true
  type             = "kubernetes.io/dockerconfigjson"
  data_wo          = { ".dockerconfigjson" = lookup(var.registry_nvcrio_dockerconfigs, each.key, null) }
  data_wo_revision = tonumber(each.key)
  lifecycle {
    precondition {
      condition     = try(can(jsondecode(var.registry_nvcrio_dockerconfigs[each.key])), false)
      error_message = "Every retained DCGM registry generation requires its escrowed Docker configuration."
    }
    prevent_destroy = true
  }
  depends_on = [terraform_data.cluster_contract, terraform_data.credential_migration_gate]
}
