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
          archive_timeout                     = "60s"
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
    kubernetes_secret_v1.database_account_versioned,
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
    marker_preparation_enabled   = var.prepare_database_restore_marker_job
    restore_verification_enabled = var.run_database_restore_verification_job
    marker_cleanup_enabled       = var.cleanup_database_restore_marker_job
  }

}

resource "terraform_data" "postgresql_pitr_marker_contract" {
  count = var.prepare_database_restore_marker_job ? 1 : 0

  input = {
    source_cluster     = "fs2-control-db"
    source_backup_name = var.database_restore_source_backup_name
    source_backup_time = var.database_restore_source_backup_time
    marker_id          = var.database_restore_marker_id
    marker_job         = "fs2-control-db-pitr-marker"
    output_contract    = "payload-free job log line FS2_PITR_TARGET_TIME=<RFC3339>"
    next_step          = "disable marker preparation and enable recovery with the captured target time"
  }
}

# This bounded, separately enabled Job runs only after the operator records a
# successful base Backup. It creates a non-sensitive table, grants the existing
# verifier SELECT on only that table, commits marker A, emits the target time,
# then commits marker B. A subsequent apply restores to the emitted time and
# must observe A present and B absent, proving replay past the base backup and a
# real point-in-time boundary.
resource "kubernetes_job_v1" "database_pitr_marker" {
  count = var.prepare_database_restore_marker_job && var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = "fs2-control-db-pitr-marker"
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "database-pitr-marker"
    })
    annotations = {
      "fs2.nebius.ai/source-backup-name" = var.database_restore_source_backup_name
      "fs2.nebius.ai/source-backup-time" = var.database_restore_source_backup_time
      "fs2.nebius.ai/non-sensitive"      = "marker-id-timestamp-and-wal-lsn-only"
    }
  }

  spec {
    backoff_limit           = 0
    active_deadline_seconds = 300

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "database-pitr-marker"
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
          name    = "mark"
          image   = local.postgresql_image
          command = ["/bin/sh", "-ceu"]
          args = [<<-SCRIPT
            psql --host=fs2-control-db-rw.fs2-data.svc.cluster.local --port=5432 --dbname=fs2serve --no-password --no-psqlrc --set=ON_ERROR_STOP=1 --set=marker_id="$PITR_MARKER_ID" <<'SQL'
            BEGIN;
            CREATE TABLE IF NOT EXISTS public.fs2_pitr_restore_markers (
              marker_id text PRIMARY KEY CHECK (marker_id ~ '^[a-z0-9][a-z0-9-]{7,62}-(a|b)$'),
              phase text NOT NULL CHECK (phase IN ('before-target', 'after-target')),
              created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              wal_lsn pg_lsn NOT NULL
            );
            REVOKE ALL ON TABLE public.fs2_pitr_restore_markers FROM PUBLIC;
            REVOKE CREATE ON SCHEMA public FROM fs2_serve_restore_verifier;
            GRANT USAGE ON SCHEMA public TO fs2_serve_restore_verifier;
            GRANT SELECT ON TABLE public.fs2_pitr_restore_markers TO fs2_serve_restore_verifier;
            SELECT count(*) = 0 AS safe_prepare
            FROM public.fs2_pitr_restore_markers
            \gset
            \if :safe_prepare
            \else
              \echo 'marker preparation refused: prior marker rows require cleanup'
              \quit 1
            \endif
            INSERT INTO public.fs2_pitr_restore_markers(marker_id, phase, wal_lsn)
            VALUES (:'marker_id' || '-a', 'before-target', pg_current_wal_insert_lsn());
            COMMIT;
            SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS pitr_target_time \gset
            \echo FS2_PITR_TARGET_TIME=:pitr_target_time
            SELECT pg_sleep(2);
            INSERT INTO public.fs2_pitr_restore_markers(marker_id, phase, wal_lsn)
            VALUES (:'marker_id' || '-b', 'after-target', pg_current_wal_insert_lsn());
            SELECT marker_id, phase, created_at, wal_lsn
            FROM public.fs2_pitr_restore_markers
            WHERE marker_id IN (:'marker_id' || '-a', :'marker_id' || '-b')
            ORDER BY created_at;
            SQL
          SCRIPT
          ]

          env {
            name  = "PITR_MARKER_ID"
            value = var.database_restore_marker_id
          }
          env {
            name = "PGUSER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["owner"].metadata[0].name
                key  = "username"
              }
            }
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["owner"].metadata[0].name
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
            secret_name = "fs2-control-db-ca"
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
  timeouts { create = "10m" }

  depends_on = [
    kubernetes_manifest.control_database_scheduled_backup,
    terraform_data.postgresql_pitr_marker_contract,
  ]
}

resource "terraform_data" "postgresql_restore_verification_contract" {
  count = var.run_database_restore_verification_job ? 1 : 0

  input = {
    source_cluster     = "fs2-control-db"
    source_backup_name = var.database_restore_source_backup_name
    source_backup_time = var.database_restore_source_backup_time
    recovery_cluster   = "fs2-control-db-restore-verification"
    verifier_job       = "fs2-control-db-restore-verifier"
    marker_id          = var.database_restore_marker_id
    target_time        = var.database_restore_target_time
    expected_before    = "${var.database_restore_marker_id}-a"
    expected_after     = "${var.database_restore_marker_id}-b"
  }

  lifecycle {
    precondition {
      condition     = var.postgresql_backup.enabled
      error_message = "Database restore verification requires the durable PostgreSQL backup contract."
    }
  }
}

# A restore test must prove the backup can create a different PostgreSQL
# cluster and replay archived WAL to the operator-captured point between marker
# A and marker B. Probing the source primary or stopping at the end of the base
# backup would prove only base-backup readability.
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
            targetTime = var.database_restore_target_time
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
              psql --host=fs2-control-db-restore-verification-rw.fs2-data.svc.cluster.local --port=5432 --dbname=fs2serve --no-password --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 --set=marker_id="$PITR_MARKER_ID" --set=target_time="$PITR_TARGET_TIME" --command="$1"
            }
            test "$(query "SELECT current_user = 'fs2_serve_restore_verifier_login'")" = "t"
            test "$(query "SELECT current_database() = 'fs2serve'")" = "t"
            test "$(query "SELECT to_regclass('public.fs2_schema_migrations') IS NOT NULL")" = "t"
            test "$(query "SELECT has_table_privilege(current_user, 'public.fs2_pitr_restore_markers', 'SELECT')")" = "t"
            test "$(query "SELECT NOT has_table_privilege(current_user, 'public.fs2_pitr_restore_markers', 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')")" = "t"
            test "$(query "SELECT NOT has_schema_privilege(current_user, 'public', 'CREATE')")" = "t"
            test "$(query "SELECT count(*) = 1 AND bool_and(phase = 'before-target' AND created_at <= :'target_time'::timestamptz AND wal_lsn <= pg_last_wal_replay_lsn()) FROM public.fs2_pitr_restore_markers WHERE marker_id = :'marker_id' || '-a'")" = "t"
            test "$(query "SELECT count(*) = 0 FROM public.fs2_pitr_restore_markers WHERE marker_id = :'marker_id' || '-b'")" = "t"
            test "$(query "SELECT pg_last_wal_replay_lsn() IS NOT NULL")" = "t"
            test "$(query "SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m') AND (c.relname = ANY (ARRAY['fs2_tokens','fs2_operations','fs2_operation_events','fs2_audit_events','fs2_request_debug','fs2_scientific_artifacts','fs2_scientific_uploads','fs2_scientific_artifact_events','fs2_scientific_run_results','fs2_operator_sessions']) OR c.relname ~ '(credential|secret|token|session|operation|audit|payload|artifact)' OR EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attname ~ '(credential|secret|token|session|payload|request|response|artifact|document|detail)')) AND (has_table_privilege(current_user,c.oid,'SELECT') OR has_any_column_privilege(current_user,c.oid,'SELECT')))")" = "t"
            printf '%s\n' 'restore verification passed: archived WAL replay reached marker A, excluded marker B, and sensitive tables remain denied'
          SCRIPT
          ]

          env {
            name  = "PITR_MARKER_ID"
            value = var.database_restore_marker_id
          }
          env {
            name  = "PITR_TARGET_TIME"
            value = var.database_restore_target_time
          }

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

resource "terraform_data" "postgresql_pitr_marker_cleanup_contract" {
  count = var.cleanup_database_restore_marker_job ? 1 : 0

  input = {
    source_cluster     = "fs2-control-db"
    source_backup_name = var.database_restore_source_backup_name
    source_backup_time = var.database_restore_source_backup_time
    marker_id          = var.database_restore_marker_id
    target_time        = var.database_restore_target_time
    cleanup_job        = "fs2-control-db-pitr-marker-cleanup"
    cleanup_scope      = "exact marker A/B pair and marker-only table/grant"
  }
}

# Marker cleanup is a distinct supervised apply after the recovery verifier has
# succeeded. It refuses an incomplete or mixed marker table, then drops the
# non-sensitive table atomically. The normal no-acceptance apply removes this
# completed Job after its payload-free receipt has been captured.
resource "kubernetes_job_v1" "database_pitr_marker_cleanup" {
  count = var.cleanup_database_restore_marker_job && var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = "fs2-control-db-pitr-marker-cleanup"
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "database-pitr-marker-cleanup"
    })
    annotations = {
      "fs2.nebius.ai/source-backup-name" = var.database_restore_source_backup_name
      "fs2.nebius.ai/source-backup-time" = var.database_restore_source_backup_time
      "fs2.nebius.ai/target-time"        = var.database_restore_target_time
      "fs2.nebius.ai/non-sensitive"      = "exact-marker-pair-cleanup"
    }
  }

  spec {
    backoff_limit           = 0
    active_deadline_seconds = 300

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "database-pitr-marker-cleanup"
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
          name    = "cleanup"
          image   = local.postgresql_image
          command = ["/bin/sh", "-ceu"]
          args = [<<-SCRIPT
            psql --host=fs2-control-db-rw.fs2-data.svc.cluster.local --port=5432 --dbname=fs2serve --no-password --no-psqlrc --set=ON_ERROR_STOP=1 --set=marker_id="$PITR_MARKER_ID" <<'SQL'
            BEGIN;
            LOCK TABLE public.fs2_pitr_restore_markers IN ACCESS EXCLUSIVE MODE;
            SELECT (
              count(*) = 2 AND
              count(*) FILTER (WHERE marker_id IN (:'marker_id' || '-a', :'marker_id' || '-b')) = 2 AND
              bool_and(
                (marker_id = :'marker_id' || '-a' AND phase = 'before-target') OR
                (marker_id = :'marker_id' || '-b' AND phase = 'after-target')
              )
            ) AS safe_cleanup
            FROM public.fs2_pitr_restore_markers
            \gset
            \if :safe_cleanup
            \else
              \echo 'marker cleanup refused: table is not the exact verified pair'
              \quit 1
            \endif
            REVOKE ALL ON TABLE public.fs2_pitr_restore_markers FROM fs2_serve_restore_verifier;
            DROP TABLE public.fs2_pitr_restore_markers;
            COMMIT;
            \echo 'marker cleanup passed: exact non-sensitive marker pair and marker-only grant removed'
            SQL
          SCRIPT
          ]

          env {
            name  = "PITR_MARKER_ID"
            value = var.database_restore_marker_id
          }
          env {
            name = "PGUSER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["owner"].metadata[0].name
                key  = "username"
              }
            }
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_account["owner"].metadata[0].name
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
            secret_name = "fs2-control-db-ca"
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
  timeouts { create = "10m" }

  depends_on = [
    kubernetes_manifest.control_database_scheduled_backup,
    terraform_data.postgresql_pitr_marker_cleanup_contract,
  ]
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
  }

  depends_on = [kubernetes_manifest.control_database]
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
  }

  depends_on = [kubernetes_manifest.control_database]
}
