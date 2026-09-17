locals {
  alertmanager_service_name = "fs2-${var.run_id}-monitoring-alertmanager"
  # kube-prometheus-stack owns this stable datasource and its provisioning
  # file. A second sidecar ConfigMap with the same datasource.yaml key races
  # the chart-owned file and can leave the run-scoped UID absent in Grafana.
  alertmanager_grafana_datasource = "alertmanager"
  tempo_service_name              = "fs2-tempo"
  tempo_grafana_datasource        = "fs2-${var.run_id}-tempo"
  loki_tenant_id                  = "fs2-platform"
}

# Loki's tenant header is meaningful only behind a network identity boundary.
# Select the single-binary workload and admit the three application consumers,
# its Prometheus health scraper, and Loki's own cluster ports. Model/scientific
# namespaces match none of these peers and therefore cannot query port 3100.
resource "kubernetes_network_policy_v1" "loki_ingress" {
  metadata {
    name      = "fs2-loki-ingress"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels    = local.common_labels
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component" = "single-binary"
        "app.kubernetes.io/instance"  = "fs2-${var.run_id}-loki"
        "app.kubernetes.io/name"      = "loki"
      }
    }
    policy_types = ["Ingress"]

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "grafana"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "fs2-${var.run_id}-otel-gateway"
            "app.kubernetes.io/name"     = "opentelemetry-collector"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component" = "gateway"
            "app.kubernetes.io/part-of"   = "fs2-serve"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    # Preserve Loki self-monitoring without admitting any workload namespace.
    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "prometheus"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component" = "single-binary"
            "app.kubernetes.io/instance"  = "fs2-${var.run_id}-loki"
            "app.kubernetes.io/name"      = "loki"
          }
        }
      }
      ports {
        port     = "7946"
        protocol = "TCP"
      }
      ports {
        port     = "7946"
        protocol = "UDP"
      }
      ports {
        port     = "9095"
        protocol = "TCP"
      }
    }
  }

  depends_on = [helm_release.loki]
}

# Single-binary Tempo is deliberately sized for the cluster-local seven-day
# trace/debug window. Durable accounting remains a workloads-stage database
# concern; Tempo is the raw correlation plane for request and Job attempts.
resource "helm_release" "tempo" {
  name             = "fs2-${var.run_id}-tempo"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "tempo"
  version          = local.chart_versions.tempo
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 1200

  # Chart archive SHA-256 at the pinned repository URL:
  # f1f6e318d5bca3b5097cb676077796cdf8135beb2c1f71c4d14614ccf9b0081b
  values = [
    file("${path.module}/values/tempo.yaml"),
    yamlencode({
      serviceMonitor = {
        additionalLabels = { release = "fs2-${var.run_id}-monitoring" }
      }
    }),
  ]

  depends_on = [helm_release.monitoring]
}

# Kubernetes Events must have one active watcher. Keeping this separate from
# the node log DaemonSet avoids duplicate events while preserving node-local
# container-log collection and checkpoint behavior.
resource "helm_release" "otel_cluster" {
  name             = "fs2-${var.run_id}-otel-cluster"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-collector"
  version          = local.chart_versions.opentelemetry
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/otel-cluster.yaml")]

  depends_on = [helm_release.otel_gateway]
}

resource "kubernetes_config_map_v1" "grafana_tempo_datasource" {
  metadata {
    name      = "fs2-tempo-grafana-datasource"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels = merge(local.common_labels, {
      grafana_datasource = "1"
    })
  }

  data = {
    "datasource.yaml" = yamlencode({
      apiVersion = 1
      prune      = false
      datasources = [{
        name      = local.tempo_grafana_datasource
        uid       = local.tempo_grafana_datasource
        type      = "tempo"
        access    = "proxy"
        orgId     = 1
        url       = "http://${local.tempo_service_name}.fs2-observability.svc.cluster.local:3200"
        isDefault = false
        editable  = false
        version   = 1
        jsonData = {
          httpMethod = "GET"
          tracesToLogsV2 = {
            datasourceUid      = "fs2-${var.run_id}-loki"
            filterBySpanID     = true
            filterByTraceID    = true
            spanStartTimeShift = "-1m"
            spanEndTimeShift   = "1m"
          }
          serviceMap = {
            datasourceUid = "prometheus"
          }
        }
      }]
    })
  }

  depends_on = [
    helm_release.monitoring,
    helm_release.tempo,
  ]
}

output "observability_operator_contract" {
  description = "Non-secret installed services and Grafana datasource identities consumed by the workloads admin projection."
  value = {
    schema = "fs2-serve.nebius.ai/observability-operator/v1"
    alertmanager = {
      enabled                = var.alertmanager.enabled
      service_name           = local.alertmanager_service_name
      service_port           = 9093
      grafana_datasource_uid = var.alertmanager.enabled ? local.alertmanager_grafana_datasource : null
      retention              = var.alertmanager.retention
      storage = {
        class_name   = var.alertmanager.storage.storage_class_name
        size_gib     = var.alertmanager.storage.size_gib
        when_deleted = "Retain"
        when_scaled  = "Retain"
      }
    }
    tempo = {
      enabled                = true
      service_name           = local.tempo_service_name
      service_port           = 3200
      grafana_datasource_uid = local.tempo_grafana_datasource
    }
    loki = {
      auth_enabled           = true
      service_name           = "fs2-loki"
      service_port           = 3100
      tenant_id              = local.loki_tenant_id
      ingress_policy_name    = kubernetes_network_policy_v1.loki_ingress.metadata[0].name
    }
    raw_backends_public = false
    operator_surface    = "grafana-native-auth"
  }
}
