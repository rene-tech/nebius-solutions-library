resource "helm_release" "cert_manager" {
  name             = "fs2-${var.run_id}-cert-manager"
  namespace        = kubernetes_namespace_v1.platform["cert-manager"].metadata[0].name
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = local.chart_versions.cert_manager
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [yamlencode({
    crds = { enabled = true }
    config = {
      gatewayAPI = {
        enabled = true
      }
    }
  })]

  # cert-manager discovers Gateway API support only when its controller
  # starts. Envoy Gateway is the sole CRD owner, so its complete Gateway API
  # bundle must exist before this release starts on a fresh cluster.
  depends_on = [helm_release.envoy_gateway]
}

resource "helm_release" "cloudnative_pg" {
  name             = "fs2-${var.run_id}-cloudnative-pg"
  namespace        = kubernetes_namespace_v1.platform["cnpg-system"].metadata[0].name
  repository       = "https://cloudnative-pg.github.io/charts"
  chart            = "cloudnative-pg"
  version          = local.chart_versions.cloudnative_pg
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/cloudnative-pg.yaml")]

  depends_on = [terraform_data.kueue_deployment_admission_ready]
}

resource "helm_release" "envoy_gateway" {
  name             = "fs2-${var.run_id}-envoy"
  namespace        = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
  repository       = "oci://docker.io/envoyproxy"
  chart            = "gateway-helm"
  version          = local.chart_versions.envoy_gateway
  create_namespace = false
  # Envoy Gateway v1.8.3's CRD subchart is the single owner of its complete
  # Gateway API v1.5.1 and Envoy-specific CRD bundle. Install it before
  # cert-manager so Gateway API support is available at controller startup.
  skip_crds       = false
  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  depends_on = [terraform_data.cluster_contract]
}

resource "helm_release" "kueue" {
  name             = "fs2-${var.run_id}-kueue"
  namespace        = kubernetes_namespace_v1.platform["kueue-system"].metadata[0].name
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = local.chart_versions.kueue
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  # Helm forces install/upgrade readiness waiting when atomic=true. Keep the
  # provider's literal wait flag false so destroy does not run Helm's generic
  # WaitForDelete loop after every Kueue delete request has succeeded.
  wait    = false
  timeout = 900

  # Kueue registers a Job admission webhook. Cert-manager's post-install
  # startup API check is itself a Job, so let that check finish before Kueue
  # can register its webhook and briefly make Job admission unavailable.
  depends_on = [helm_release.cert_manager]
}

# KEDA is installed once by the foundation. Static workloads leave it dormant;
# the explicit keda workload mode creates one ScaledObject per routed model.
# Kueue remains restricted to asynchronous Jobs/Workloads and never shares
# replica ownership with serving Deployments.
resource "helm_release" "keda" {
  name             = "fs2-${var.run_id}-keda"
  namespace        = kubernetes_namespace_v1.platform["keda"].metadata[0].name
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = local.chart_versions.keda
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/keda.yaml")]

  depends_on = [terraform_data.kueue_deployment_admission_ready]
}

resource "helm_release" "kserve_crd" {
  name             = "fs2-${var.run_id}-kserve-crd"
  namespace        = kubernetes_namespace_v1.platform["kserve"].metadata[0].name
  repository       = "oci://ghcr.io/kserve/charts"
  chart            = "kserve-crd"
  version          = local.chart_versions.kserve_crd
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  depends_on = [
    helm_release.cert_manager,
    helm_release.envoy_gateway,
    terraform_data.kueue_deployment_admission_ready,
  ]
}

resource "helm_release" "kserve_resources" {
  name             = "fs2-${var.run_id}-kserve"
  namespace        = kubernetes_namespace_v1.platform["kserve"].metadata[0].name
  repository       = "oci://ghcr.io/kserve/charts"
  chart            = "kserve-resources"
  version          = local.chart_versions.kserve_resources
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  depends_on = [
    helm_release.kserve_crd,
    terraform_data.kueue_deployment_admission_ready,
  ]
}

resource "helm_release" "monitoring" {
  name             = "fs2-${var.run_id}-monitoring"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = local.chart_versions.kube_prometheus_stack
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 1200

  values = [
    yamlencode({
      fullnameOverride = "fs2-${var.run_id}-monitoring"
      # No Alertmanager release or external link is part of this slice.
      alertmanager = { enabled = false }
      grafana = {
        admin = {
          existingSecret = var.grafana_admin_secret_ref.name
          userKey        = var.grafana_admin_secret_ref.user_key
          passwordKey    = var.grafana_admin_secret_ref.password_key
        }
        sidecar = {
          dashboards = { enabled = true }
          datasources = {
            enabled    = true
            label      = "grafana_datasource"
            labelValue = "1"
          }
        }
      }
      prometheus = {
        # Kueue 0.17.8's chart can create a ServiceMonitor but cannot express
        # metric relabeling. Own this bounded monitor in the Prometheus release:
        # raw Workload identity is dropped, while canonical LocalQueue/model
        # dimensions remain available for bounded Grafana history.
        additionalServiceMonitors = [{
          name = "fs2-${var.run_id}-kueue-bounded"
          namespaceSelector = {
            matchNames = ["kueue-system"]
          }
          selector = {
            matchLabels = {
              "app.kubernetes.io/component" = "metrics-service"
              "app.kubernetes.io/instance"  = helm_release.kueue.name
              "app.kubernetes.io/name"      = "kueue"
            }
          }
          sampleLimit           = 20000
          labelLimit            = 32
          labelNameLengthLimit  = 128
          labelValueLengthLimit = 256
          endpoints = [{
            bearerTokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"
            interval        = "30s"
            path            = "/metrics"
            port            = "https"
            scheme          = "https"
            scrapeTimeout   = "10s"
            tlsConfig       = { insecureSkipVerify = true }
            metricRelabelings = [
              { action = "drop", sourceLabels = ["workload"], regex = ".+" },
              { action = "drop", sourceLabels = ["workload_name"], regex = ".+" },
              {
                action = "labeldrop"
                regex  = "(?i)^(gpu_uuid|uuid|pod_uid|pod|operation|operation_id|principal|token|tenant|workload|workload_name)$"
              },
            ]
          }]
        }]
        prometheusSpec = {
          serviceMonitorSelectorNilUsesHelmValues = false
          podMonitorSelectorNilUsesHelmValues     = false
          serviceMonitorNamespaceSelector = {
            matchExpressions = [{
              key      = "kubernetes.io/metadata.name"
              operator = "In"
              values = [
                "fs2-observability",
                "fs2-system",
                "fs2-models",
                "fs2-data",
                "cnpg-system",
                "kube-system",
                "kueue-system",
                "keda",
              ]
            }]
          }
          podMonitorNamespaceSelector = {
            matchExpressions = [{
              key      = "kubernetes.io/metadata.name"
              operator = "In"
              values = [
                "fs2-observability",
                "fs2-system",
                "fs2-models",
                "fs2-data",
                "cnpg-system",
                "kube-system",
                "kueue-system",
                "keda",
              ]
            }]
          }
        }
      }
    }),
    yamlencode(local.grafana_publication_values),
  ]

  depends_on = [
    terraform_data.kueue_deployment_admission_ready,
    kubernetes_secret_v1.grafana_admin,
  ]
}

# The Kueue endpoint performs SubjectAccessReview for /metrics. Bind only that
# non-resource GET role to the Prometheus service account; discovery remains
# owned by kube-prometheus-stack.
resource "kubernetes_cluster_role_binding_v1" "kueue_metrics_reader" {
  metadata {
    name = "fs2-${var.run_id}-kueue-prometheus-metrics-reader"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "${helm_release.kueue.name}-metrics-reader"
  }

  subject {
    kind      = "ServiceAccount"
    name      = "fs2-${var.run_id}-monitoring-prometheus"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  }

  depends_on = [
    helm_release.kueue,
    helm_release.monitoring,
  ]
}

resource "helm_release" "loki" {
  name             = "fs2-${var.run_id}-loki"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "loki"
  version          = local.chart_versions.loki
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 1200

  values = [file("${path.module}/values/loki.yaml")]

  depends_on = [helm_release.monitoring]
}

resource "helm_release" "otel_gateway" {
  name             = "fs2-${var.run_id}-otel-gateway"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-collector"
  version          = local.chart_versions.opentelemetry
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/otel-gateway.yaml")]

  depends_on = [helm_release.loki]
}

resource "helm_release" "otel_node" {
  name             = "fs2-${var.run_id}-otel-node"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-collector"
  version          = local.chart_versions.opentelemetry
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/otel-node.yaml")]

  depends_on = [helm_release.otel_gateway]
}
