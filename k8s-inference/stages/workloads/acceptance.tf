resource "kubernetes_network_policy_v1" "acceptance_admin" {
  count = var.run_acceptance_job ? 1 : 0

  metadata {
    name      = "fs2-terraform-acceptance-admin"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  spec {
    pod_selector {
      match_labels = { "app.kubernetes.io/name" = "fs2-serve-control-plane" }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector {
          match_labels = { "app.kubernetes.io/component" = "acceptance" }
        }
      }
      ports {
        port     = "8080"
        protocol = "TCP"
      }
    }
  }
}

resource "kubernetes_job_v1" "authenticated_edge_acceptance" {
  count = var.run_acceptance_job ? 1 : 0

  metadata {
    name      = "fs2-terraform-edge-acceptance"
    namespace = "fs2-system"
    labels    = local.common_labels
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 600

    template {
      metadata {
        labels = merge(local.common_labels, { "app.kubernetes.io/component" = "acceptance" })
      }
      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false

        container {
          name    = "probe"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["python", "-c", file("${path.module}/scripts/acceptance.py")]

          env {
            name  = "FS2_BASE_URL"
            value = local.public_edge_enabled ? local.public_base_url : "http://fs2-serve-control-plane.fs2-system.svc.cluster.local:8080"
          }
          env {
            name  = "FS2_ORIGIN"
            value = local.public_base_url
          }
          env {
            name  = "FS2_INTERNAL_URL"
            value = "http://fs2-serve-control-plane.fs2-system.svc.cluster.local:8080"
          }
          env {
            name = "FS2_ADMIN_TOKEN"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.admin.metadata[0].name
                key  = "token"
              }
            }
          }

          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities { drop = ["ALL"] }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts { create = "15m" }

  depends_on = [
    helm_release.control_plane,
    kubernetes_network_policy_v1.acceptance_admin,
  ]
}

resource "kubernetes_job_v1" "reporting_datasource_acceptance" {
  count = var.run_acceptance_job ? 1 : 0

  metadata {
    name      = "fs2-terraform-reporting-acceptance"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 600

    template {
      metadata {
        labels = merge(local.common_labels, { "app.kubernetes.io/component" = "acceptance" })
      }
      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        node_selector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/pool"   = "system"
        }

        container {
          name    = "probe"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["python", "-c", file("${path.module}/scripts/reporting_acceptance.py")]

          env {
            name = "FS2_REPORTING_DATABASE_URL"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_consumer["reporting"].metadata[0].name
                key  = "url"
              }
            }
          }

          env {
            name  = "FS2_GRAFANA_URL"
            value = local.grafana_internal_url
          }

          env {
            name  = "FS2_GRAFANA_PROMETHEUS_DATASOURCE_UID"
            value = "prometheus"
          }

          env {
            name  = "FS2_GRAFANA_LOKI_DATASOURCE_UID"
            value = local.grafana_loki_datasource_uid
          }

          env {
            name  = "FS2_RUN_ID"
            value = var.run_id
          }

          env {
            name = "FS2_GRAFANA_USER"
            value_from {
              secret_key_ref {
                name = data.kubernetes_secret_v1.grafana_admin.metadata[0].name
                key  = data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.user_key
              }
            }
          }

          env {
            name = "FS2_GRAFANA_PASSWORD"
            value_from {
              secret_key_ref {
                name = data.kubernetes_secret_v1.grafana_admin.metadata[0].name
                key  = data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.password_key
              }
            }
          }

          volume_mount {
            name       = "database-ca"
            mount_path = "/tls"
            read_only  = true
          }

          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi" }
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
            secret_name = kubernetes_secret_v1.database_consumer["reporting"].metadata[0].name
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
  timeouts { create = "15m" }

  depends_on = [helm_release.control_plane, kubernetes_secret_v1.grafana_datasource]
}

resource "kubernetes_job_v1" "gpu_observability_acceptance" {
  count = var.run_acceptance_job && var.deployment_profile == "full_catalog" ? 1 : 0

  metadata {
    name      = "fs2-terraform-gpu-observability-acceptance"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }

  spec {
    backoff_limit           = 2
    active_deadline_seconds = 900

    template {
      metadata {
        labels = merge(local.common_labels, { "app.kubernetes.io/component" = "acceptance" })
      }
      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        node_selector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/pool"   = "system"
        }

        container {
          name    = "probe"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["python", "-c", file("${path.module}/scripts/gpu_observability_acceptance.py")]

          env {
            name  = "FS2_PROMETHEUS_URL"
            value = "http://fs2-${var.run_id}-monitoring-prometheus.fs2-observability.svc.cluster.local:9090"
          }

          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities { drop = ["ALL"] }
          }
        }
      }
    }
  }

  wait_for_completion = true
  timeouts { create = "20m" }

  depends_on = [helm_release.dcgm_exporter]
}

resource "kubernetes_manifest" "kueue_admission_acceptance" {
  count = var.run_acceptance_job ? 1 : 0

  manifest = {
    apiVersion = "batch/v1"
    kind       = "Job"
    metadata = {
      name      = "fs2-terraform-kueue-acceptance"
      namespace = "fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component" = "acceptance"
        "kueue.x-k8s.io/queue-name"   = local.selected_accelerator_pool_profile.queue.local_queue_name
      })
    }
    spec = {
      suspend               = true
      backoffLimit          = 1
      activeDeadlineSeconds = 600
      completions           = 1
      parallelism           = 1
      completionMode        = "NonIndexed"
      template = {
        metadata = {
          labels = merge(local.common_labels, { "app.kubernetes.io/component" = "acceptance" })
        }
        spec = {
          restartPolicy                = "Never"
          automountServiceAccountToken = false
          nodeSelector = local.selected_queue_pools[
            local.selected_accelerator_pool_profile.queue.acceptance_pool_id
          ].scheduling.stable_node_labels
          tolerations = local.selected_queue_pools[
            local.selected_accelerator_pool_profile.queue.acceptance_pool_id
          ].scheduling.tolerations
          containers = [{
            name    = "probe"
            image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
            command = ["python", "-c", "print('KUEUE_ADMITTED')"]
            resources = {
              requests = { cpu = "50m", memory = "64Mi" }
              limits   = { cpu = "100m", memory = "128Mi" }
            }
            securityContext = {
              allowPrivilegeEscalation = false
              readOnlyRootFilesystem   = true
              runAsNonRoot             = true
              capabilities             = { drop = ["ALL"] }
            }
          }]
        }
      }
    }
  }

  # The Job and Kueue controllers mutate these fields while the provider is
  # waiting for completion. Marking only those controller-owned fields as
  # computed prevents an inconsistent-result error without relaxing drift
  # detection for the Job's queue label, node placement, image, or command.
  # An explicit list replaces the provider defaults, deliberately keeping the
  # top-level Job metadata Terraform-owned.
  computed_fields = [
    "spec.suspend",
    "spec.template.metadata.annotations",
    "spec.template.metadata.labels",
  ]

  wait {
    condition {
      type   = "Complete"
      status = "True"
    }
  }

  timeouts { create = "15m" }

  depends_on = [kubernetes_manifest.model_local_queue]
}
