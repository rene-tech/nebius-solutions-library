# Executable SAI-06 backup/restore observability. CNPG supplies backup,
# recoverability and WAL metrics. A payload-free exporter inventories all
# current and non-current S3 versions so the versioned bucket's real pressure,
# not merely its current objects, is alertable. It also observes the durable
# restore-verification success receipt prefix.

locals {
  postgresql_backup_metrics_name = "fs2-postgresql-backup-metrics"
  postgresql_backup_metrics_labels = merge(local.common_labels, {
    "app.kubernetes.io/name"      = local.postgresql_backup_metrics_name
    "app.kubernetes.io/component" = "postgresql-backup-observability"
  })
}

resource "kubernetes_config_map_v1" "postgresql_backup_metrics" {
  count = var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = local.postgresql_backup_metrics_name
    namespace = "fs2-data"
    labels    = local.postgresql_backup_metrics_labels
  }

  data = {
    "postgresql_backup_metrics.py" = file("${path.module}/scripts/postgresql_backup_metrics.py")
  }
}

resource "kubernetes_deployment_v1" "postgresql_backup_metrics" {
  count = var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = local.postgresql_backup_metrics_name
    namespace = "fs2-data"
    labels    = local.postgresql_backup_metrics_labels
  }

  spec {
    replicas = 1
    selector {
      match_labels = {
        "app.kubernetes.io/name" = local.postgresql_backup_metrics_name
      }
    }
    template {
      metadata { labels = local.postgresql_backup_metrics_labels }
      spec {
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
          name    = "metrics"
          image   = local.postgresql_image
          command = ["python3", "/opt/fs2/postgresql_backup_metrics.py"]

          port {
            name           = "metrics"
            container_port = 9188
          }
          env {
            name  = "S3_ENDPOINT"
            value = var.postgresql_backup.storage_contract.object_storage.endpoint
          }
          env {
            name  = "S3_BUCKET"
            value = var.postgresql_backup.storage_contract.object_storage.name
          }
          env {
            name  = "S3_PREFIX"
            value = "postgresql/v1/"
          }
          env {
            name  = "BUCKET_CAPACITY_BYTES"
            value = tostring(var.postgresql_backup.storage_contract.sizing.configured_capacity_gib * 1024 * 1024 * 1024)
          }
          env {
            name = "AWS_ACCESS_KEY_ID"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgresql_backup[0].metadata[0].name
                key  = "ACCESS_KEY_ID"
              }
            }
          }
          env {
            name = "AWS_SECRET_ACCESS_KEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgresql_backup[0].metadata[0].name
                key  = "ACCESS_SECRET_KEY"
              }
            }
          }
          env {
            name = "AWS_REGION"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgresql_backup[0].metadata[0].name
                key  = "AWS_REGION"
              }
            }
          }

          volume_mount {
            name       = "script"
            mount_path = "/opt/fs2"
            read_only  = true
          }
          resources {
            requests = { cpu = "25m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi" }
          }
          readiness_probe {
            http_get {
              path = "/metrics"
              port = "metrics"
            }
            initial_delay_seconds = 5
            period_seconds        = 30
            timeout_seconds       = 10
            failure_threshold     = 3
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = "metrics"
            }
            initial_delay_seconds = 10
            period_seconds        = 30
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities { drop = ["ALL"] }
          }
        }
        volume {
          name = "script"
          config_map {
            name         = kubernetes_config_map_v1.postgresql_backup_metrics[0].metadata[0].name
            default_mode = "0555"
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "postgresql_backup_metrics" {
  count = var.postgresql_backup.enabled ? 1 : 0

  metadata {
    name      = local.postgresql_backup_metrics_name
    namespace = "fs2-data"
    labels    = local.postgresql_backup_metrics_labels
  }
  spec {
    selector = { "app.kubernetes.io/name" = local.postgresql_backup_metrics_name }
    port {
      name        = "metrics"
      port        = 9188
      target_port = "metrics"
    }
  }
}

resource "kubernetes_manifest" "postgresql_backup_service_monitor" {
  count = var.postgresql_backup.enabled ? 1 : 0

  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "ServiceMonitor"
    metadata = {
      name      = local.postgresql_backup_metrics_name
      namespace = "fs2-data"
      labels = merge(local.common_labels, {
        release = "fs2-${var.run_id}-monitoring"
      })
    }
    spec = {
      selector          = { matchLabels = { "app.kubernetes.io/name" = local.postgresql_backup_metrics_name } }
      namespaceSelector = { matchNames = ["fs2-data"] }
      endpoints         = [{ port = "metrics", path = "/metrics", interval = "60s", scrapeTimeout = "15s" }]
    }
  }
}

resource "kubernetes_manifest" "postgresql_backup_prometheus_rule" {
  count = var.postgresql_backup.enabled ? 1 : 0

  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "PrometheusRule"
    metadata = {
      name      = "fs2-postgresql-backup-resilience"
      namespace = "fs2-data"
      labels = merge(local.common_labels, {
        release = "fs2-${var.run_id}-monitoring"
      })
      annotations = {
        "fs2.nebius.ai/restore-receipt-source" = "postgresql/v1/restore-verification/success/"
      }
    }
    spec = {
      groups = [{
        name = "fs2-postgresql-backup-resilience"
        rules = [
          {
            alert       = "Fs2PostgresqlBackupFailed"
            expr        = "max(cnpg_collector_last_failed_backup_timestamp{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}) > max(cnpg_collector_last_available_backup_timestamp{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"})"
            for         = "5m"
            labels      = { severity = "critical" }
            annotations = { summary = "The newest CloudNativePG backup attempt failed" }
          },
          {
            alert       = "Fs2PostgresqlBackupStale"
            expr        = "(max(cnpg_collector_last_available_backup_timestamp{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}) == 0) or absent(cnpg_collector_last_available_backup_timestamp{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}) or (time() - max(cnpg_collector_last_available_backup_timestamp{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}) > 129600)"
            for         = "15m"
            labels      = { severity = "critical" }
            annotations = { summary = "No successful daily PostgreSQL backup exists within 36 hours" }
          },
          {
            alert       = "Fs2PostgresqlRecoverabilityPointMissing"
            expr        = "(max(cnpg_collector_first_recoverability_point{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}) == 0) or absent(cnpg_collector_first_recoverability_point{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"})"
            for         = "15m"
            labels      = { severity = "critical" }
            annotations = { summary = "CloudNativePG has no first recoverability point" }
          },
          {
            alert       = "Fs2PostgresqlWalArchiveFailed"
            expr        = "sum(increase(cnpg_pg_stat_archiver_failed_count{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\"}[10m])) > 0"
            for         = "1m"
            labels      = { severity = "critical" }
            annotations = { summary = "PostgreSQL WAL archiving reported failures" }
          },
          {
            alert       = "Fs2PostgresqlWalArchiveStalled"
            expr        = "max(cnpg_collector_pg_wal_archive_status{namespace=\"fs2-data\",pod=~\"fs2-control-db-[0-9]+\",value=\"ready\"}) > 0"
            for         = "10m"
            labels      = { severity = "critical" }
            annotations = { summary = "PostgreSQL WAL files have remained ready but unarchived" }
          },
          {
            alert       = "Fs2PostgresqlBackupBucketExporterUnavailable"
            expr        = "absent(fs2_postgresql_backup_bucket_scrape_success{namespace=\"fs2-data\"}) or max(fs2_postgresql_backup_bucket_scrape_success{namespace=\"fs2-data\"}) == 0"
            for         = "10m"
            labels      = { severity = "critical" }
            annotations = { summary = "The version-aware PostgreSQL backup bucket inventory is unavailable" }
          },
          {
            alert       = "Fs2PostgresqlBackupBucketCapacityWarning"
            expr        = "max(fs2_postgresql_backup_bucket_usage_ratio{namespace=\"fs2-data\"}) > 0.80"
            for         = "15m"
            labels      = { severity = "warning" }
            annotations = { summary = "PostgreSQL backup bucket is above 80 percent of configured capacity" }
          },
          {
            alert       = "Fs2PostgresqlBackupBucketCapacityCritical"
            expr        = "max(fs2_postgresql_backup_bucket_usage_ratio{namespace=\"fs2-data\"}) > 0.90"
            for         = "5m"
            labels      = { severity = "critical" }
            annotations = { summary = "PostgreSQL backup bucket is above 90 percent of configured capacity" }
          },
          {
            alert       = "Fs2PostgresqlRestoreVerificationFailed"
            expr        = "max(kube_job_status_failed{namespace=\"fs2-data\",job_name=~\"fs2-control-db-restore-verifier|fs2-control-db-restore-receipt\"}) > 0"
            for         = "1m"
            labels      = { severity = "critical" }
            annotations = { summary = "The PostgreSQL PITR verification or durable receipt job failed" }
          },
          {
            alert       = "Fs2PostgresqlRestoreVerificationStale"
            expr        = "(max(fs2_postgresql_restore_last_success_timestamp_seconds{namespace=\"fs2-data\"}) == 0) or absent(fs2_postgresql_restore_last_success_timestamp_seconds{namespace=\"fs2-data\"}) or (time() - max(fs2_postgresql_restore_last_success_timestamp_seconds{namespace=\"fs2-data\"}) > 604800)"
            for         = "15m"
            labels      = { severity = "critical" }
            annotations = { summary = "No successful payload-free PITR verification receipt exists within seven days" }
          },
        ]
      }]
    }
  }

  depends_on = [
    kubernetes_manifest.control_database,
    kubernetes_manifest.postgresql_backup_service_monitor,
  ]
}
