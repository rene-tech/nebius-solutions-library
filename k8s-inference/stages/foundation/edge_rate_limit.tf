locals {
  edge_rate_limit_redis_name = "fs2-edge-rate-limit-redis"
  # Immutable linux/amd64 manifest recorded by docker-library/repo-info for
  # Redis 8.10.0. This source pin still requires the normal independent image
  # scan and promotion gate before any deployment.
  edge_rate_limit_redis_image = "docker.io/library/redis@sha256:e4f640a477d2a0b45eefbf16c67359d8decf4290d8b693d59141654eff47e989"
  edge_rate_limit_redis_labels = merge(local.common_labels, {
    "app.kubernetes.io/name"      = local.edge_rate_limit_redis_name
    "app.kubernetes.io/component" = "edge-rate-limit-store"
  })
}

# Global rate-limit counters are deliberately ephemeral: losing this Pod may
# reset a window but cannot lose customer or operator data. A PDB and rolling
# surge avoid voluntary single-Pod gaps; Envoy retains independent connection
# caps and application token budgets if the store is unavailable.
resource "kubernetes_deployment_v1" "edge_rate_limit_redis" {
  metadata {
    name      = local.edge_rate_limit_redis_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    replicas = 1

    selector {
      match_labels = local.edge_rate_limit_redis_labels
    }

    strategy {
      type = "RollingUpdate"
      rolling_update {
        max_surge       = 1
        max_unavailable = 0
      }
    }

    template {
      metadata {
        labels = local.edge_rate_limit_redis_labels
      }

      spec {
        automount_service_account_token = false
        node_selector = {
          "workload.fs2.nebius/system" = "true"
        }

        security_context {
          run_as_non_root = true
          run_as_user     = 999
          run_as_group    = 999
          fs_group        = 999
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        container {
          name              = "redis"
          image             = local.edge_rate_limit_redis_image
          image_pull_policy = "IfNotPresent"
          command           = ["redis-server"]
          args = [
            "--bind", "0.0.0.0",
            "--protected-mode", "no",
            "--save", "",
            "--appendonly", "no",
            "--maxmemory", "192mb",
            "--maxmemory-policy", "allkeys-lru",
          ]

          port {
            name           = "redis"
            container_port = 6379
            protocol       = "TCP"
          }

          resources {
            requests = {
              cpu    = "50m"
              memory = "64Mi"
            }
            limits = {
              cpu    = "250m"
              memory = "256Mi"
            }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities {
              drop = ["ALL"]
            }
          }

          readiness_probe {
            tcp_socket {
              port = 6379
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          liveness_probe {
            tcp_socket {
              port = 6379
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }

        topology_spread_constraint {
          max_skew           = 1
          topology_key       = "kubernetes.io/hostname"
          when_unsatisfiable = "ScheduleAnyway"
          label_selector {
            match_labels = local.edge_rate_limit_redis_labels
          }
        }

        volume {
          name = "data"
          empty_dir {
            size_limit = "512Mi"
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "edge_rate_limit_redis" {
  metadata {
    name      = local.edge_rate_limit_redis_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    selector = local.edge_rate_limit_redis_labels
    port {
      name        = "redis"
      port        = 6379
      target_port = "redis"
      protocol    = "TCP"
    }
  }
}

resource "kubernetes_pod_disruption_budget_v1" "edge_rate_limit_redis" {
  metadata {
    name      = local.edge_rate_limit_redis_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    min_available = "1"
    selector {
      match_labels = local.edge_rate_limit_redis_labels
    }
  }
}

resource "kubernetes_network_policy_v1" "edge_rate_limit_redis" {
  metadata {
    name      = local.edge_rate_limit_redis_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    pod_selector {
      match_labels = local.edge_rate_limit_redis_labels
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        pod_selector {
          match_labels = {
            "fs2.nebius.ai/edge-rate-limit-service" = "true"
          }
        }
      }
      ports {
        port     = "6379"
        protocol = "TCP"
      }
    }
  }
}
