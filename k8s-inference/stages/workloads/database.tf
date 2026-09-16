locals {
  database_role_memberships = {
    runtime            = "fs2_serve_runtime"
    maintenance        = "fs2_serve_maintenance"
    activation         = "fs2_serve_activation"
    storage            = "fs2_serve_storage"
    storage_disclosure = "fs2_serve_storage_disclosure"
    restore_verifier   = "fs2_serve_restore_verifier"
    reporting          = "fs2_serve_reporting"
    monitoring         = "pg_monitor"
  }

  database_group_roles = [
    "fs2_serve_runtime",
    "fs2_serve_maintenance",
    "fs2_serve_activation",
    "fs2_serve_storage",
    "fs2_serve_storage_disclosure",
    "fs2_serve_restore_verifier",
    "fs2_serve_reporting",
  ]
}

resource "kubernetes_manifest" "control_database" {
  manifest = {
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "Cluster"
    metadata = {
      name      = "fs2-control-db"
      namespace = "fs2-data"
      labels    = local.common_labels
    }
    spec = {
      instances = var.deployment_profile == "full_catalog" ? 3 : 1
      imageName = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
      bootstrap = {
        initdb = {
          database      = "fs2serve"
          owner         = "fs2serve"
          secret        = { name = kubernetes_secret_v1.database_account["owner"].metadata[0].name }
          encoding      = "UTF8"
          localeCType   = "C.UTF-8"
          localeCollate = "C.UTF-8"
          dataChecksums = true
        }
      }
      storage = {
        size         = var.deployment_profile == "full_catalog" ? "100Gi" : "32Gi"
        storageClass = "compute-csi-default-sc"
      }
      managed = {
        roles = concat(
          [for role in local.database_group_roles : {
            name   = role
            ensure = "present"
            login  = false
          }],
          [for account, group in local.database_role_memberships : {
            name   = local.database_accounts[account].username
            ensure = "present"
            login  = true
            passwordSecret = {
              name = kubernetes_secret_v1.database_account[account].metadata[0].name
            }
            inRoles = [group]
          }],
          [for identity in values(local.database_versioned_accounts) : {
            name   = identity.username
            ensure = "present"
            login  = true
            passwordSecret = {
              name = kubernetes_secret_v1.database_account_versioned["${identity.generation}:${identity.account}"].metadata[0].name
            }
            inRoles = [local.database_role_memberships[identity.account]]
          }]
        )
      }
      postgresql = {
        enableAlterSystem = false
        parameters = {
          max_connections                     = "300"
          password_encryption                 = "scram-sha-256"
          ssl_min_protocol_version            = "TLSv1.3"
          ssl_max_protocol_version            = "TLSv1.3"
          log_min_duration_statement          = "1000"
          idle_in_transaction_session_timeout = "60s"
          statement_timeout                   = "60s"
        }
        pg_hba = concat(
          [for account in values(local.database_accounts) : "hostssl fs2serve ${account.username} all scram-sha-256"],
          [for identity in values(local.database_versioned_accounts) : "hostssl fs2serve ${identity.username} all scram-sha-256"],
        )
      }
      resources = {
        requests = { cpu = "1", memory = "2Gi" }
        limits   = { cpu = "4", memory = "8Gi" }
      }
      affinity = {
        nodeSelector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/type"   = "regular"
          "capacity.fs2.nebius/pool"   = "system"
        }
      }
      monitoring = { enablePodMonitor = true }
    }
  }

  # CloudNativePG admission expands this map with operator-owned PostgreSQL
  # defaults. Terraform still submits every configured parameter above, while
  # all fields outside this map remain checked against the applied object.
  # Listing the provider's two metadata defaults preserves their behavior when
  # the additional computed field is configured explicitly.
  computed_fields = [
    "metadata.annotations",
    "metadata.labels",
    "spec.postgresql.parameters",
  ]

  wait {
    fields = {
      "status.phase" = "Cluster in healthy state"
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
  }

  depends_on = [
    kubernetes_secret_v1.database_account,
    kubernetes_secret_v1.database_account_versioned,
  ]
}

data "kubernetes_secret_v1" "database_ca" {
  metadata {
    name      = "fs2-control-db-ca"
    namespace = "fs2-data"
  }
  depends_on = [kubernetes_manifest.control_database, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "database_consumer" {
  for_each = local.consumer_database_secrets

  metadata {
    name      = each.value.secret_name
    namespace = each.value.namespace
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-purpose" = each.key })
    annotations = {
      "fs2.nebius.ai/credential-generation" = tostring(var.credential_generations.database)
    }
  }

  type = "Opaque"
  data_wo = {
    url = format(
      "postgresql://%s:%s@fs2-control-db-rw.fs2-data.svc.cluster.local:5432/fs2serve?sslmode=verify-full&sslrootcert=/tls/ca.crt",
      local.active_database_usernames[each.value.account],
      urlencode(local.active_database_passwords[each.value.account]),
    )
    "ca.crt" = data.kubernetes_secret_v1.database_ca.data["ca.crt"]
  }
  data_wo_revision = var.credential_generations.database

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [kubernetes_manifest.control_database, terraform_data.credential_migration_gate]
}

resource "kubernetes_secret_v1" "grafana_datasource" {
  metadata {
    name      = "fs2-serve-postgres-grafana-datasource"
    namespace = "fs2-observability"
    labels = merge(local.common_labels, {
      "grafana_datasource"               = "1"
      "fs2.nebius.ai/credential-purpose" = "reporting-datasource"
      "fs2.nebius.ai/secret-delivery"    = "terraform-disposable-bootstrap"
    })
    annotations = {
      "fs2.nebius.ai/credential-generation" = tostring(var.credential_generations.database)
    }
  }

  type = "Opaque"
  data_wo = {
    "datasource.yaml" = yamlencode({
      apiVersion = 1
      prune      = false
      datasources = [
        {
          name      = "fs2-serve-reporting"
          uid       = "fs2-serve-reporting"
          type      = "postgres"
          access    = "proxy"
          orgId     = 1
          url       = "fs2-control-db-rw.fs2-data.svc:5432"
          user      = local.active_database_usernames["reporting"]
          isDefault = false
          editable  = false
          version   = 1
          jsonData = {
            database               = "fs2serve"
            sslmode                = "verify-full"
            tlsConfigurationMethod = "file-content"
            tlsAuthWithCACert      = true
            tlsSkipVerify          = false
            maxOpenConns           = 10
            maxIdleConns           = 2
            maxIdleConnsAuto       = false
            connMaxLifetime        = 300
            postgresVersion        = 1800
            timescaledb            = false
          }
          secureJsonData = {
            password  = local.active_database_passwords["reporting"]
            tlsCACert = data.kubernetes_secret_v1.database_ca.data["ca.crt"]
          }
        },
        {
          name      = local.grafana_loki_datasource_uid
          uid       = local.grafana_loki_datasource_uid
          type      = "loki"
          access    = "proxy"
          orgId     = 1
          url       = local.grafana_loki_datasource_url
          isDefault = false
          editable  = false
          version   = 1
          jsonData = {
            maxLines = 1000
          }
        },
      ]
    })
  }
  data_wo_revision = var.credential_generations.database

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [kubernetes_manifest.control_database, terraform_data.credential_migration_gate]
}
