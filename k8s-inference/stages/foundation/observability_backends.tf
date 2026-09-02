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
        name      = "fs2-${var.run_id}-tempo"
        uid       = "fs2-${var.run_id}-tempo"
        type      = "tempo"
        access    = "proxy"
        orgId     = 1
        url       = "http://fs2-tempo.fs2-observability.svc.cluster.local:3200"
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
