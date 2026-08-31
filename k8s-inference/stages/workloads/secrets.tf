locals {
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
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "ngc_api_key" {
  count = local.ngc_api_key_required ? 1 : 0

  metadata {
    name      = "ngc-api-key"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  type = "Opaque"
  data = {
    NGC_API_KEY = var.ngc_api_key
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "nvcrio_cred" {
  count = local.model_nvcr_credentials_required ? 1 : 0

  metadata {
    name      = "nvcrio-cred"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = var.nvcrio_dockerconfigjson
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_secret_v1" "dcgm_exporter_nvcrio" {
  count = local.dcgm_nvcr_credentials_required ? 1 : 0

  metadata {
    name      = "fs2-dcgm-exporter-nvcrio"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = var.nvcrio_dockerconfigjson
  }
  depends_on = [terraform_data.cluster_contract]
}
