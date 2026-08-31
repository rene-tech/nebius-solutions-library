locals {
  # The foundation contract exposes either the fresh run-scoped Grafana
  # Service or the retained Service override. Both share the same Helm release
  # prefix as Loki, so this keeps the selector exact without a topology flag or
  # a broad label match.
  grafana_observability_release_prefix = trimsuffix(
    local.grafana_publication.service_name,
    "-monitoring-grafana",
  )
  grafana_loki_instance_label = "${local.grafana_observability_release_prefix}-loki"
  grafana_loki_datasource_uid = "fs2-${var.run_id}-loki"
  grafana_loki_datasource_url = "http://fs2-loki.fs2-observability.svc.cluster.local:3100"
  grafana_internal_url        = "http://${local.grafana_publication.service_name}.fs2-observability.svc.cluster.local"
}

resource "kubernetes_network_policy_v1" "grafana_observability_egress" {
  metadata {
    name      = "fs2-grafana-to-observability-egress"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }

  # Grafana is the only public observability pane. Its data plane and datasource
  # sidecar need only DNS, the canonical in-cluster Kubernetes API Service,
  # the reporting database, Prometheus, and Loki.
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "grafana"
      }
    }
    policy_types = ["Egress"]

    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
        pod_selector {
          match_expressions {
            key      = "k8s-app"
            operator = "In"
            values   = ["coredns", "kube-dns"]
          }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    dynamic "egress" {
      for_each = sort(tolist(local.kubernetes_api_service_cidrs))
      iterator = kubernetes_api_cidr

      content {
        to {
          ip_block {
            cidr = kubernetes_api_cidr.value
          }
        }
        ports {
          port     = "443"
          protocol = "TCP"
        }
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-data"
          }
        }
        pod_selector {
          match_labels = {
            "cnpg.io/cluster" = "fs2-control-db"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    egress {
      to {
        pod_selector {
          # The kube-prometheus-stack chart owns exactly one Prometheus
          # workload in this namespace. Its instance label differs between
          # the retained release (fs2-monitoring-prometheus) and run-scoped
          # Terraform releases, while this chart label is stable in both.
          match_labels = {
            "app.kubernetes.io/name" = "prometheus"
          }
        }
      }
      ports {
        port     = "9090"
        protocol = "TCP"
      }
    }

    egress {
      to {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component" = "single-binary"
            "app.kubernetes.io/instance"  = local.grafana_loki_instance_label
            "app.kubernetes.io/name"      = "loki"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = endswith(local.grafana_publication.service_name, "-monitoring-grafana")
      error_message = "Grafana's Service must retain the reviewed monitoring-grafana suffix used to derive the exact Loki release label."
    }

    precondition {
      condition     = can(cidrhost(local.kubernetes_api_service_cidr, 0))
      error_message = "The default/kubernetes spec.clusterIP must produce one exact /32 or /128 for the Grafana datasource sidecar watch."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "helm_release" "dcgm_exporter" {
  count = var.deployment_profile == "full_catalog" ? 1 : 0

  name             = "fs2-dcgm-exporter"
  namespace        = "fs2-observability"
  repository       = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart            = "dcgm-exporter"
  version          = "4.8.3"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  # Chart archive SHA-256 at the pinned repository URL:
  # b1206338d5c446126e233f93df80f0538c285ce40b2e72e6c2f46c9db59ef223
  values = [
    file("${path.module}/values/dcgm-exporter.yaml"),
    yamlencode({
      imagePullSecrets = [{ name = kubernetes_secret_v1.dcgm_exporter_nvcrio[0].metadata[0].name }]
      config           = local.dcgm_cadence_profile.helmValues.config
      serviceMonitor = merge(
        local.dcgm_cadence_profile.helmValues.serviceMonitor,
        { additionalLabels = { release = "fs2-${var.run_id}-monitoring" } },
      )
    }),
  ]

  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_secret_v1.dcgm_exporter_nvcrio,
  ]
}
