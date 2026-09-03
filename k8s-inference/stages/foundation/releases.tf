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

# The infrastructure stage attaches the same Nebius shared filesystem to every
# worker and mounts it at /mnt/fs2cache. This cluster-scoped CSI driver turns
# that existing mount into dynamically provisioned ReadWriteMany volumes. The
# release name is deliberately stable: the upstream chart derives the public
# csi-mounted-fs-path-sc StorageClass name from it, and model bundles bind that
# exact class without making it the cluster default for databases or logs.
resource "helm_release" "filesystem_csi" {
  name             = "csi-mounted-fs-path"
  namespace        = data.kubernetes_namespace_v1.kube_system.metadata[0].name
  repository       = "oci://cr.eu-north1.nebius.cloud/mk8s/helm"
  chart            = "csi-mounted-fs-path"
  version          = local.chart_versions.filesystem_csi
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [yamlencode({
    dataDir = "/mnt/fs2cache/csi-mounted-fs-path-data/"
    affinity = {
      nodeAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = {
          nodeSelectorTerms = [{
            matchExpressions = [{
              key      = "storage.fs2.nebius/shared-cache"
              operator = "In"
              values   = ["true"]
            }]
          }]
        }
      }
    }
    tolerations = [{ operator = "Exists" }]
  })]

  depends_on = [terraform_data.cluster_contract]
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

data "external" "kueue_chart" {
  program = ["${path.module}/../../modules/jobset-controller/scripts/materialize-chart.sh"]

  query = {
    chart_ref      = local.kueue_release.chart_ref
    chart_digest   = local.kueue_release.chart_digest
    archive_sha256 = local.kueue_release.chart_archive_sha256
    chart_name     = "kueue"
    run_root       = local.normalized_run_root
  }
}

resource "terraform_data" "kueue_release_verified" {
  input = local.kueue_release

  triggers_replace = {
    chart_digest         = local.kueue_release.chart_digest
    chart_archive_sha256 = local.kueue_release.chart_archive_sha256
    chart_archive        = local.kueue_chart_archive
    image                = local.kueue_release.image
    verifier_sha256      = filesha256("${path.module}/scripts/materialize-kueue-release.sh")
  }

  provisioner "local-exec" {
    command = "\"${path.module}/scripts/materialize-kueue-release.sh\""
    quiet   = true

    environment = {
      FS2_KUEUE_CHART_REF            = local.kueue_release.chart_ref
      FS2_KUEUE_CHART_DIGEST         = local.kueue_release.chart_digest
      FS2_KUEUE_CHART_ARCHIVE_SHA256 = local.kueue_release.chart_archive_sha256
      FS2_KUEUE_IMAGE                = local.kueue_release.image
      FS2_KUEUE_RUN_ROOT             = local.normalized_run_root
      FS2_KUEUE_CHART_ARCHIVE        = local.kueue_chart_archive
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "helm_release" "kueue" {
  name             = "fs2-${var.run_id}-kueue"
  namespace        = kubernetes_namespace_v1.platform["kueue-system"].metadata[0].name
  chart            = local.kueue_chart_archive
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  # Helm forces install/upgrade readiness waiting when atomic=true. Keep the
  # provider's literal wait flag false so destroy does not run Helm's generic
  # WaitForDelete loop after every Kueue delete request has succeeded.
  wait    = false
  timeout = 900

  values = [yamlencode(local.kueue_effective_values)]

  lifecycle {
    precondition {
      condition = (
        # registry/name:tag@sha256:hex has three colons, so the tag and digest
        # are recovered the way the verifier does rather than by splitting on
        # every colon.
        local.kueue_values.controllerManager.manager.image.repository == local.kueue_image_repository &&
        local.kueue_values.controllerManager.manager.image.tag == local.kueue_image_tag &&
        local.kueue_values.controllerManager.nodeSelector["workload.fs2.nebius/system"] == "true"
      )
      error_message = "values/kueue.yaml must pin the same digest-qualified controller image and system node placement as the verified release."
    }

    precondition {
      condition = alltrue([
        for prefix in local.kueue_excluded_resource_prefixes : alltrue([
          for resource_name in local.kueue_accelerator_resource_names :
          !startswith(resource_name, prefix)
        ])
      ])
      error_message = "A Kueue excluded resource prefix would also exclude an accelerator resource this deployment budgets; auxiliary device resources must be named so they cannot shadow accelerator accounting."
    }

    # The rendered exclusions must match the core-admission choice exactly, so
    # the published contract's statement about what is enforced is always true.
    precondition {
      condition = (
        contains(local.kueue_excluded_resource_prefixes, "ephemeral-storage") &&
        (
          var.kueue.budget_core_resources
          ? alltrue([
            for prefix in local.kueue_excluded_resource_prefixes : alltrue([
              for core_name in local.kueue_core_resource_prefixes :
              !startswith(core_name, prefix)
            ])
          ])
          : length(setsubtract(
            toset(local.kueue_core_resource_prefixes),
            toset(local.kueue_excluded_resource_prefixes),
          )) == 0
        )
      )
      error_message = "The rendered Kueue exclusions must match the core-admission choice exactly. Kueue compares each prefix with a literal prefix match, so with core admission on no exclusion may be a prefix of cpu or memory (\"c\" and \"mem\" would silently exclude them), and with core admission off both must be excluded. ephemeral-storage stays excluded either way."
    }
  }

  # Kueue registers a Job admission webhook. Cert-manager's post-install
  # startup API check is itself a Job, so let that check finish before Kueue
  # can register its webhook and briefly make Job admission unavailable.
  depends_on = [
    helm_release.cert_manager,
    module.jobset_controller,
    terraform_data.kueue_release_verified,
  ]
}

module "jobset_controller" {
  count  = var.jobset.enabled ? 1 : 0
  source = "../../modules/jobset-controller"

  enabled            = true
  run_id             = var.run_id
  namespace          = kubernetes_namespace_v1.platform["jobset-system"].metadata[0].name
  kubeconfig_path    = var.kubeconfig_path
  kube_context       = var.kube_context
  run_root           = var.run_root
  cluster_id         = var.cluster_id
  kubernetes_version = var.jobset.kubernetes_version
  labels             = local.common_labels

  depends_on = [kubernetes_namespace_v1.platform["jobset-system"]]
}

# KEDA is installed once by the foundation. Static workloads leave it dormant;
# the explicit keda workload mode creates one ScaledObject per routed model.
# Kueue's Deployment integration gates each serving Pod against its exact-pool
# ResourceFlavor quota; KEDA remains the only writer of burst replica counts.
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
                "fs2-reference-data",
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

  depends_on = [
    helm_release.loki,
    helm_release.tempo,
  ]
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
