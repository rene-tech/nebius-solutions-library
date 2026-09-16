locals {
  postgresql_image              = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
  postgresql_backup_secret_name = "fs2-control-db-backup"
  postgresql_backup_credential_identity = var.postgresql_backup.enabled ? join("|", [
    var.postgresql_backup.object_storage_access.key_id,
    var.postgresql_backup.object_storage_access.access_key_id,
    var.postgresql_backup.object_storage_access.secret_reference_id,
    tostring(var.postgresql_backup.object_storage_access.resource_version),
  ]) : ""
  postgresql_backup_credential_revision = var.postgresql_backup.enabled ? (
    var.postgresql_backup.credential_generation * 16777216 +
    parseint(substr(sha256(local.postgresql_backup_credential_identity), 0, 6), 16)
  ) : 0
  postgresql_backup_barman_object_store = var.postgresql_backup.enabled ? {
    destinationPath = var.postgresql_backup.storage_contract.layout.destination_path
    endpointURL     = var.postgresql_backup.storage_contract.object_storage.endpoint
    serverName      = var.postgresql_backup.storage_contract.layout.server_name
    s3Credentials = {
      accessKeyId = {
        name = local.postgresql_backup_secret_name
        key  = "ACCESS_KEY_ID"
      }
      secretAccessKey = {
        name = local.postgresql_backup_secret_name
        key  = "ACCESS_SECRET_KEY"
      }
      region = {
        name = local.postgresql_backup_secret_name
        key  = "AWS_REGION"
      }
    }
    data = {
      compression         = "gzip"
      immediateCheckpoint = false
      jobs                = 2
    }
    wal = {
      compression = "gzip"
      maxParallel = 4
    }
  } : null

  database_role_memberships = {
    runtime          = "fs2_serve_runtime"
    maintenance      = "fs2_serve_maintenance"
    activation       = "fs2_serve_activation"
    restore_verifier = "fs2_serve_restore_verifier"
    reporting        = "fs2_serve_reporting"
    monitoring       = "pg_monitor"
  }

  database_group_roles = [
    "fs2_serve_runtime",
    "fs2_serve_maintenance",
    "fs2_serve_activation",
    "fs2_serve_restore_verifier",
    "fs2_serve_reporting",
  ]
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "postgresql_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  secret_id = var.postgresql_backup.object_storage_access.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "postgresql_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = local.postgresql_backup_secret_name
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "postgresql-backup"
    })
    annotations = {
      "fs2.nebius.ai/postgresql-backup-credential-revision"   = tostring(local.postgresql_backup_credential_revision)
      "fs2.nebius.ai/postgresql-backup-credential-generation" = tostring(var.postgresql_backup.credential_generation)
      "fs2.nebius.ai/postgresql-backup-access-key-id"         = var.postgresql_backup.object_storage_access.access_key_id
    }
  }

  type = "Opaque"
  data_wo = {
    ACCESS_KEY_ID     = var.postgresql_backup.object_storage_access.access_key_id
    ACCESS_SECRET_KEY = ephemeral.nebius_mysterybox_v1_secret_payload_entry.postgresql_backup[0].data.string_value
    AWS_REGION        = var.postgresql_backup.storage_contract.region
  }
  data_wo_revision = local.postgresql_backup_credential_revision

  depends_on = [terraform_data.cluster_contract]
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
    spec = merge({
      instances = 3
      imageName = local.postgresql_image
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
        pg_hba = [for account in values(local.database_accounts) : "hostssl fs2serve ${account.username} all scram-sha-256"]
      }
      resources = {
        requests = { cpu = "1", memory = "2Gi" }
        limits   = { cpu = "4", memory = "8Gi" }
      }
      affinity = {
        enablePodAntiAffinity = true
        podAntiAffinityType   = "required"
        topologyKey           = "kubernetes.io/hostname"
        nodeSelector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/type"   = "regular"
          "capacity.fs2.nebius/pool"   = "system"
        }
      }
      monitoring = { enablePodMonitor = true }
      }, var.postgresql_backup.enabled ? {
      backup = {
        barmanObjectStore = local.postgresql_backup_barman_object_store
        retentionPolicy   = "${var.postgresql_backup.retention_days}d"
        target            = "prefer-standby"
      }
    } : {})
  }

  # CloudNativePG admission expands this map with operator-owned PostgreSQL
  # defaults. Terraform still submits every configured parameter above, while
  # all fields outside this map remain checked against the applied object.
  # Listing the provider's two metadata defaults preserves their behavior when
  # the additional computed field is configured explicitly.
  computed_fields = [
    "metadata.annotations",
    "metadata.labels",
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
    kubernetes_secret_v1.postgresql_backup,
  ]
}

resource "kubernetes_manifest" "control_database_scheduled_backup" {
  count = var.postgresql_backup.enabled ? 1 : 0

  manifest = {
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "ScheduledBackup"
    metadata = {
      name      = "fs2-control-db"
      namespace = "fs2-data"
      labels    = local.common_labels
    }
    spec = {
      backupOwnerReference = "self"
      cluster              = { name = "fs2-control-db" }
      immediate            = true
      method               = "barmanObjectStore"
      schedule             = var.postgresql_backup.schedule
      suspend              = false
      target               = "prefer-standby"
    }
  }

  computed_fields = [
    "metadata.annotations",
    "metadata.labels",
  ]

  depends_on = [kubernetes_manifest.control_database]
}

resource "terraform_data" "postgresql_backup_contract" {
  count = var.postgresql_backup.enabled ? 1 : 0

  input = {
    schema                       = "fs2-serve.nebius.ai/postgresql-backup-runtime/v1"
    cluster_name                 = "fs2-control-db"
    scheduled_backup_name        = "fs2-control-db"
    destination_path             = var.postgresql_backup.storage_contract.layout.destination_path
    server_name                  = var.postgresql_backup.storage_contract.layout.server_name
    retention_days               = var.postgresql_backup.retention_days
    schedule                     = var.postgresql_backup.schedule
    wal_archiving                = true
    credential_secret            = "fs2-data/${local.postgresql_backup_secret_name}"
    credential_delivery          = "MYSTERY_BOX_WRITE_ONLY"
    restore_verification_enabled = var.run_database_restore_verification_job
  }

}

resource "terraform_data" "postgresql_restore_verification_contract" {
  count = var.run_database_restore_verification_job ? 1 : 0

  input = {
    source_cluster   = "fs2-control-db"
    recovery_cluster = "fs2-control-db-restore-verification"
    verifier_job     = "fs2-control-db-restore-verifier"
  }

  lifecycle {
    precondition {
      condition     = var.postgresql_backup.enabled
      error_message = "Database restore verification requires the durable PostgreSQL backup contract."
    }
  }
}

# A restore test must prove the backup can create a different PostgreSQL
# cluster; probing the source primary would only test login availability. This
# temporary cluster is deliberately opt-in so the rollout can first wait for a
# successful ScheduledBackup, then recover and verify it in a second apply.
resource "kubernetes_manifest" "database_restore_verification" {
  count = var.run_database_restore_verification_job && var.postgresql_backup.enabled ? 1 : 0

  manifest = {
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "Cluster"
    metadata = {
      name      = "fs2-control-db-restore-verification"
      namespace = "fs2-data"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component" = "database-restore-verification"
      })
      annotations = {
        "fs2.nebius.ai/cleanup" = "set acceptance.verify_database_restore=false and re-apply workloads"
      }
    }
    spec = {
      instances = 1
      imageName = local.postgresql_image
      bootstrap = {
        recovery = {
          source = "fs2-control-db-backup-source"
          recoveryTarget = {
            targetImmediate = true
          }
        }
      }
      externalClusters = [{
        name              = "fs2-control-db-backup-source"
        barmanObjectStore = local.postgresql_backup_barman_object_store
      }]
      storage = {
        size         = var.deployment_profile == "full_catalog" ? "100Gi" : "32Gi"
        storageClass = "compute-csi-default-sc"
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
      monitoring = { enablePodMonitor = false }
    }
  }

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
    create = "60m"
    update = "60m"
  }

  depends_on = [
    kubernetes_manifest.control_database_scheduled_backup,
    terraform_data.postgresql_backup_contract,
    terraform_data.postgresql_restore_verification_contract,
  ]
}

resource "kubernetes_job_v1" "database_restore_verification" {
  count = var.run_database_restore_verification_job && var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = "fs2-control-db-restore-verifier"
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "database-restore-verification"
    })
  }

  spec {
    backoff_limit           = 1
    active_deadline_seconds = 900

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "database-restore-verification"
        })
      }
      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        node_selector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/type"   = "regular"
          "capacity.fs2.nebius/pool"   = "system"
        }

        security_context {
          run_as_non_root = true
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name    = "verify"
          image   = local.postgresql_image
          command = ["/bin/sh", "-ceu"]
          args = [<<-SCRIPT
            query() {
              psql --host=fs2-control-db-restore-verification-rw.fs2-data.svc.cluster.local --port=5432 --dbname=fs2serve --no-password --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="$1"
            }
            test "$(query "SELECT current_user = 'fs2_serve_restore_verifier_login'")" = "t"
            test "$(query "SELECT current_database() = 'fs2serve'")" = "t"
            test "$(query "SELECT to_regclass('public.fs2_schema_migrations') IS NOT NULL")" = "t"
            test "$(query "SELECT NOT has_table_privilege(current_user, 'public.fs2_operations', 'SELECT')")" = "t"
            printf '%s\n' 'restore verification passed: recovered schema present and verifier remains least privilege'
          SCRIPT
          ]

          env {
            name = "PGUSER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["restore_verifier"].metadata[0].name
                key  = "username"
              }
            }
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["restore_verifier"].metadata[0].name
                key  = "password"
              }
            }
          }
          env {
            name  = "PGSSLMODE"
            value = "verify-full"
          }
          env {
            name  = "PGSSLROOTCERT"
            value = "/tls/ca.crt"
          }

          volume_mount {
            name       = "database-ca"
            mount_path = "/tls"
            read_only  = true
          }

          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities { drop = ["ALL"] }
          }
        }

        volume {
          name = "database-ca"
          secret {
            secret_name = "fs2-control-db-restore-verification-ca"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
      }
    }
  }

  wait_for_completion = true
  timeouts { create = "20m" }

  depends_on = [kubernetes_manifest.database_restore_verification]
}

data "kubernetes_secret_v1" "database_ca" {
  metadata {
    name      = "fs2-control-db-ca"
    namespace = "fs2-data"
  }
  depends_on = [kubernetes_manifest.control_database]
}

resource "kubernetes_secret_v1" "database_consumer" {
  for_each = local.consumer_database_secrets

  metadata {
    name      = each.value.secret_name
    namespace = each.value.namespace
    labels    = merge(local.common_labels, { "fs2.nebius.ai/credential-purpose" = each.key })
  }

  type = "Opaque"
  data = {
    url = format(
      "postgresql://%s:%s@fs2-control-db-rw.fs2-data.svc.cluster.local:5432/fs2serve?sslmode=verify-full&sslrootcert=/tls/ca.crt",
      local.database_accounts[each.value.account].username,
      urlencode(random_password.database[each.value.account].result),
    )
    "ca.crt" = data.kubernetes_secret_v1.database_ca.data["ca.crt"]
  }
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
  }

  type = "Opaque"
  data = {
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
          user      = local.database_accounts["reporting"].username
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
            password  = random_password.database["reporting"].result
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
}
