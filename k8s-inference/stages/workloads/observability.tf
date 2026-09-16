locals {
  otel_node_relay_path                = "${path.module}/../foundation/values/otel-node-relay.yaml"
  otel_node_relay                     = file(local.otel_node_relay_path)
  otel_node_relay_sha256              = sha256(local.otel_node_relay)
  otel_node_config_map_name           = "fs2-otel-node-relay-${substr(local.otel_node_relay_sha256, 0, 16)}"
  dcgm_metrics_path                   = "${path.module}/values/dcgm-metrics.csv"
  dcgm_metrics                        = file(local.dcgm_metrics_path)
  dcgm_metrics_sha256                 = sha256(local.dcgm_metrics)
  dcgm_metrics_config_name            = "fs2-dcgm-metrics-${substr(local.dcgm_metrics_sha256, 0, 16)}"
  dcgm_cold_config                    = local.dcgm_cadence_contract.profiles.coldStartCampaign.helmValues.config.data
  dcgm_cold_config_sha256             = sha256(local.dcgm_cold_config)
  dcgm_cold_config_map_name           = "fs2-dcgm-config-${substr(local.dcgm_cold_config_sha256, 0, 16)}"
  node_agents_use_exception_namespace = var.pod_security_rollout_phase != "rollback-remove-exception"
  legacy_host_agents_enabled = contains([
    "prepare",
    "bootstrap-baseline",
    "rollback-restore-host-agents",
    "rollback-remove-exception",
  ], var.pod_security_rollout_phase)
  exception_host_agents_enabled = var.pod_security_rollout_phase != "rollback-remove-exception"
  node_observability_namespace = (
    local.node_agents_use_exception_namespace ?
    "fs2-node-observability" :
    "fs2-observability"
  )
  gpu_observer_namespace = (
    local.node_agents_use_exception_namespace ?
    "fs2-node-observability" :
    "fs2-system"
  )
  gpu_observer_additional_namespaces = contains([
    "prepare",
    "bootstrap-baseline",
    "rollback-restore-host-agents",
  ], var.pod_security_rollout_phase) ? ["fs2-system"] : []
  # The foundation contract exposes either the fresh run-scoped Grafana
  # Service or the retained Service override. Both share the same Helm release
  # prefix as Loki, so this keeps the selector exact without a topology flag or
  # a broad label match.
  grafana_observability_release_prefix = trimsuffix(
    local.grafana_publication.service_name,
    "-monitoring-grafana",
  )
  grafana_loki_instance_label  = "${local.grafana_observability_release_prefix}-loki"
  grafana_loki_datasource_uid  = "fs2-${var.run_id}-loki"
  grafana_loki_datasource_url  = "http://fs2-loki.fs2-observability.svc.cluster.local:3100"
  grafana_tempo_instance_label = "${local.grafana_observability_release_prefix}-tempo"
  grafana_internal_url         = "http://${local.grafana_publication.service_name}.fs2-observability.svc.cluster.local"
}

# Both standard metrics and the optional cold-campaign configuration are
# versioned, immutable, and retained. Helm only mounts these Terraform-owned
# objects; it never creates a mutable fixed-name telemetry ConfigMap.
resource "kubernetes_config_map_v1" "dcgm_metrics" {
  # Retain both immutable generations across every rollout and rollback phase.
  # Only the Helm consumers are phase-conditioned.
  for_each = var.deployment_profile == "full_catalog" ? toset([
    "fs2-observability",
    "fs2-node-observability",
  ]) : toset([])

  metadata {
    name      = local.dcgm_metrics_config_name
    namespace = each.value
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "dcgm-metrics-config"
    })
    annotations = {
      "security.fs2.nebius.ai/content-sha256" = "sha256:${local.dcgm_metrics_sha256}"
    }
  }

  immutable = true
  data      = { metrics = local.dcgm_metrics }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_config_map_v1" "dcgm_cold_config" {
  for_each = var.deployment_profile == "full_catalog" ? toset([
    "fs2-observability",
    "fs2-node-observability",
  ]) : toset([])

  metadata {
    name      = local.dcgm_cold_config_map_name
    namespace = each.value
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "dcgm-cold-config"
    })
    annotations = {
      "security.fs2.nebius.ai/content-sha256" = "sha256:${local.dcgm_cold_config_sha256}"
    }
  }

  immutable = true
  data      = { "config.yaml" = local.dcgm_cold_config }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_network_policy_v1" "grafana_observability_egress" {
  metadata {
    name      = "fs2-grafana-to-observability-egress"
    namespace = "fs2-observability"
    labels    = local.common_labels
  }

  # Grafana is the only public observability pane. Its data plane and datasource
  # sidecar need only DNS, the canonical in-cluster Kubernetes API Service,
  # the reporting database, Prometheus, Loki, Tempo, and the optional private
  # Alertmanager datasource.
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
      for_each = sort(tolist(local.kubernetes_api_egress_cidrs))
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

    egress {
      to {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = local.grafana_tempo_instance_label
            "app.kubernetes.io/name"     = "tempo"
          }
        }
      }
      ports {
        port     = "3200"
        protocol = "TCP"
      }
    }

    dynamic "egress" {
      for_each = local.observability_operator.alertmanager.enabled ? [1] : []

      content {
        to {
          pod_selector {
            match_labels = {
              alertmanager = local.observability_operator.alertmanager.service_name
            }
          }
        }
        ports {
          port     = "9093"
          protocol = "TCP"
        }
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

resource "helm_release" "dcgm_exporter_legacy" {
  count = var.deployment_profile == "full_catalog" && local.legacy_host_agents_enabled ? 1 : 0

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
      imagePullSecrets = [{ name = kubernetes_secret_v1.dcgm_exporter_nvcrio_legacy[0].metadata[0].name }]
      arguments        = local.dcgm_cadence_profile.helmValues.arguments
      customMetrics    = ""
      config = merge(local.dcgm_cadence_profile.helmValues.config, {
        create = false
        name   = local.dcgm_cold_config_map_name
      })
      extraConfigMapVolumes = [{
        name = "exporter-metrics-volume"
        configMap = {
          name  = local.dcgm_metrics_config_name
          items = [{ key = "metrics", path = "default-counters.csv" }]
        }
      }]
      serviceMonitor = merge(
        local.dcgm_cadence_profile.helmValues.serviceMonitor,
        { additionalLabels = { release = "fs2-${var.run_id}-monitoring" } },
      )
    }),
  ]

  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_config_map_v1.dcgm_cold_config,
    kubernetes_config_map_v1.dcgm_metrics,
    kubernetes_secret_v1.dcgm_exporter_nvcrio_legacy,
  ]
}

moved {
  from = helm_release.dcgm_exporter
  to   = helm_release.dcgm_exporter_legacy[0]
}

resource "helm_release" "dcgm_exporter_exception" {
  count = var.deployment_profile == "full_catalog" && local.exception_host_agents_enabled ? 1 : 0

  name             = "fs2-dcgm-exporter-psa"
  namespace        = "fs2-node-observability"
  repository       = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart            = "dcgm-exporter"
  version          = "4.8.3"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [
    file("${path.module}/values/dcgm-exporter.yaml"),
    yamlencode({
      imagePullSecrets = [{ name = kubernetes_secret_v1.dcgm_exporter_nvcrio_exception[0].metadata[0].name }]
      arguments        = local.dcgm_cadence_profile.helmValues.arguments
      customMetrics    = ""
      config = merge(local.dcgm_cadence_profile.helmValues.config, {
        create = false
        name   = local.dcgm_cold_config_map_name
      })
      extraConfigMapVolumes = [{
        name = "exporter-metrics-volume"
        configMap = {
          name  = local.dcgm_metrics_config_name
          items = [{ key = "metrics", path = "default-counters.csv" }]
        }
      }]
      serviceMonitor = merge(
        local.dcgm_cadence_profile.helmValues.serviceMonitor,
        { additionalLabels = { release = "fs2-${var.run_id}-monitoring" } },
      )
    }),
  ]

  depends_on = [
    terraform_data.pod_security_rollout_contract,
    kubernetes_config_map_v1.dcgm_cold_config,
    kubernetes_config_map_v1.dcgm_metrics,
    kubernetes_secret_v1.dcgm_exporter_nvcrio_exception,
  ]
}
