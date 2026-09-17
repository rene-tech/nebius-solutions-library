locals {
  edge_rate_limit_redis_name          = "fs2-edge-rate-limit-redis"
  edge_rate_limit_redis_headless_name = "${local.edge_rate_limit_redis_name}-headless"
  edge_rate_limit_redis_sentinel_name = "${local.edge_rate_limit_redis_name}-sentinel"
  edge_rate_limit_redis_master_name   = "fs2-edge-rate-limit"
  # Immutable linux/amd64 manifest recorded by docker-library/repo-info for
  # Redis 8.10.0. This source pin still requires the normal independent image
  # scan and promotion gate before any deployment.
  edge_rate_limit_redis_image = "docker.io/library/redis@sha256:e4f640a477d2a0b45eefbf16c67359d8decf4290d8b693d59141654eff47e989"
  edge_rate_limit_redis_labels = merge(local.common_labels, {
    "app.kubernetes.io/name"      = local.edge_rate_limit_redis_name
    "app.kubernetes.io/component" = "edge-rate-limit-store"
  })
  edge_rate_limit_managed_resource_addresses = concat([
    "kubernetes_config_map_v1.edge_rate_limit_redis",
    "kubernetes_stateful_set_v1.edge_rate_limit_redis",
    "kubernetes_service_v1.edge_rate_limit_redis_headless",
    "kubernetes_service_v1.edge_rate_limit_redis_sentinel",
    "kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis",
    "kubernetes_network_policy_v1.edge_rate_limit_redis",
  ], local.public_edge_enabled ? [
    "terraform_data.public_edge_apply_eligibility[0]",
    "kubernetes_manifest.public_edge_node_authority_policy[0]",
    "kubernetes_manifest.public_edge_node_authority_binding[0]",
  ] : [])
}

# The rate-limit store is an ephemeral three-member Redis replication group
# supervised by three co-located Sentinels. Sentinels expose one logical write
# authority and require quorum before failover; the RLS connects to stable
# Sentinel endpoints instead of load-balancing writes across Redis Pods.
# Restarted members ask the live quorum for the current primary before Redis
# starts, preventing a StatefulSet rollout from creating independent writers.
resource "kubernetes_config_map_v1" "edge_rate_limit_redis" {
  metadata {
    name      = "${local.edge_rate_limit_redis_name}-bootstrap"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  data = {
    "configure.sh" = <<-EOT
      #!/bin/sh
      set -eu

      master_name="${local.edge_rate_limit_redis_master_name}"
      headless_service="${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"
      sentinel_service="${local.edge_rate_limit_redis_sentinel_name}.envoy-gateway-system.svc.cluster.local"
      bootstrap_master="${local.edge_rate_limit_redis_name}-0.$headless_service"
      self_address="$HOSTNAME.$headless_service"
      master_address=""
      master_port="6379"

      master_reply="$(redis-cli -h "$sentinel_service" -p 26379 --raw \
        SENTINEL get-master-addr-by-name "$master_name" 2>/dev/null || true)"
      if [ -n "$master_reply" ]; then
        master_address="$(printf '%s\n' "$master_reply" | sed -n '1p')"
        master_port="$(printf '%s\n' "$master_reply" | sed -n '2p')"
      fi
      # During a primary restart, keep Redis stopped long enough for the two
      # surviving Sentinels to agree on and publish a replacement. This avoids
      # resurrecting the old ordinal as an independent writable authority.
      if [ -n "$master_reply" ] && [ "$master_address" = "$self_address" ]; then
        attempts=0
        while [ "$attempts" -lt 30 ]; do
          sleep 1
          master_reply="$(redis-cli -h "$sentinel_service" -p 26379 --raw \
            SENTINEL get-master-addr-by-name "$master_name" 2>/dev/null || true)"
          candidate="$(printf '%s\n' "$master_reply" | sed -n '1p')"
          if [ -n "$candidate" ] && [ "$candidate" != "$self_address" ]; then
            master_address="$candidate"
            master_port="$(printf '%s\n' "$master_reply" | sed -n '2p')"
            break
          fi
          attempts=$((attempts + 1))
        done
      fi
      if [ -z "$master_address" ]; then
        master_address="$bootstrap_master"
        master_port="6379"
      fi

      mkdir -p /work/sentinel /data
      printf '%s\n' \
        'bind 0.0.0.0' \
        'protected-mode no' \
        'port 6379' \
        'dir /data' \
        'save ""' \
        'appendonly no' \
        'maxmemory 192mb' \
        'maxmemory-policy allkeys-lru' \
        'replica-read-only yes' \
        "replica-announce-ip $self_address" \
        'replica-announce-port 6379' > /work/redis.conf
      if [ "$self_address" != "$master_address" ]; then
        printf 'replicaof %s %s\n' "$master_address" "$master_port" >> /work/redis.conf
      fi

      printf '%s\n' \
        'bind 0.0.0.0' \
        'protected-mode no' \
        'port 26379' \
        'dir /work/sentinel' \
        'sentinel resolve-hostnames yes' \
        'sentinel announce-hostnames yes' \
        "sentinel announce-ip $self_address" \
        'sentinel announce-port 26379' \
        "sentinel monitor $master_name $master_address $master_port 2" \
        "sentinel down-after-milliseconds $master_name 5000" \
        "sentinel failover-timeout $master_name 15000" \
        "sentinel parallel-syncs $master_name 1" > /work/sentinel.conf
    EOT
  }
}

resource "kubernetes_stateful_set_v1" "edge_rate_limit_redis" {
  metadata {
    name      = local.edge_rate_limit_redis_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    replicas     = 3
    service_name = local.edge_rate_limit_redis_headless_name

    selector {
      match_labels = local.edge_rate_limit_redis_labels
    }

    update_strategy {
      type = "RollingUpdate"
    }

    template {
      metadata {
        labels = local.edge_rate_limit_redis_labels
      }

      spec {
        automount_service_account_token = false
        node_selector = local.public_edge_enabled ? var.public_edge_availability_contract.node_selector : {
          "workload.fs2.nebius/system" = "true"
        }

        dynamic "affinity" {
          for_each = local.public_edge_enabled ? [1] : []
          content {
            node_affinity {
              required_during_scheduling_ignored_during_execution {
                node_selector_term {
                  match_fields {
                    key      = "metadata.name"
                    operator = "In"
                    values   = local.public_edge_membership_authority.serving_member_instance_ids
                  }
                }
              }
            }
            pod_anti_affinity {
              required_during_scheduling_ignored_during_execution {
                topology_key = var.public_edge_availability_contract.topology_key
                label_selector {
                  match_labels = local.edge_rate_limit_redis_labels
                }
              }
            }
          }
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

        init_container {
          name              = "configure"
          image             = local.edge_rate_limit_redis_image
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/sh", "/bootstrap/configure.sh"]

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            capabilities {
              drop = ["ALL"]
            }
          }

          volume_mount {
            name       = "bootstrap"
            mount_path = "/bootstrap"
            read_only  = true
          }
          volume_mount {
            name       = "configuration"
            mount_path = "/work"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }

        container {
          name              = "redis"
          image             = local.edge_rate_limit_redis_image
          image_pull_policy = "IfNotPresent"
          command           = ["redis-server", "/work/redis.conf"]

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
            exec {
              command = ["redis-cli", "ping"]
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          liveness_probe {
            exec {
              command = ["redis-cli", "ping"]
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          volume_mount {
            name       = "configuration"
            mount_path = "/work"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }

        container {
          name              = "sentinel"
          image             = local.edge_rate_limit_redis_image
          image_pull_policy = "IfNotPresent"
          command           = ["redis-server", "/work/sentinel.conf", "--sentinel"]

          port {
            name           = "sentinel"
            container_port = 26379
            protocol       = "TCP"
          }

          resources {
            requests = {
              cpu    = "25m"
              memory = "32Mi"
            }
            limits = {
              cpu    = "125m"
              memory = "128Mi"
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
            exec {
              command = ["redis-cli", "-p", "26379", "ping"]
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          liveness_probe {
            exec {
              command = ["redis-cli", "-p", "26379", "ping"]
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          volume_mount {
            name       = "configuration"
            mount_path = "/work"
          }
        }

        topology_spread_constraint {
          max_skew           = 1
          min_domains        = local.public_edge_enabled ? var.public_edge_availability_contract.minimum_domains : null
          topology_key       = var.public_edge_availability_contract.topology_key
          when_unsatisfiable = local.public_edge_enabled ? "DoNotSchedule" : "ScheduleAnyway"
          label_selector {
            match_labels = local.edge_rate_limit_redis_labels
          }
        }

        volume {
          name = "bootstrap"
          config_map {
            name         = kubernetes_config_map_v1.edge_rate_limit_redis.metadata[0].name
            # 0444 in the Kubernetes API's decimal representation. The init
            # container invokes the file through /bin/sh, so execute bits are
            # intentionally unnecessary.
            default_mode = 292
          }
        }
        volume {
          name = "configuration"
          empty_dir {
            size_limit = "16Mi"
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

  lifecycle {
    precondition {
      condition = (
        !local.public_edge_enabled || try(
          data.external.public_edge_mutation_fence[0].result.verdict == "PASS" &&
          data.external.public_edge_mutation_fence[0].result.membership_payload_sha256 == local.public_edge_membership_authority.payload_sha256 &&
          data.external.public_edge_mutation_fence[0].result.membership_receipt_sha256 == local.public_edge_membership_authority.receipt_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_policy_sha256 == local.public_edge_node_authority_policy_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_binding_sha256 == local.public_edge_node_authority_binding_sha256 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.provider_member_count) >= 3 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.eligible_node_count) >= 3 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.hostname_domain_count) >= 3,
          false,
        )
      )
      error_message = "The public edge Redis/Sentinel StatefulSet requires the source-trusted provider membership receipt, continuous Node authority policy, and a fresh mutation fence with three eligible hostname domains."
    }
  }

  depends_on = [
    data.external.public_edge_mutation_fence,
    terraform_data.public_edge_apply_eligibility,
    kubernetes_service_v1.edge_rate_limit_redis_headless,
    kubernetes_service_v1.edge_rate_limit_redis_sentinel,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis,
    kubernetes_network_policy_v1.edge_rate_limit_redis,
  ]
}

resource "kubernetes_service_v1" "edge_rate_limit_redis_headless" {
  metadata {
    name      = local.edge_rate_limit_redis_headless_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    cluster_ip                  = "None"
    publish_not_ready_addresses = true
    selector                    = local.edge_rate_limit_redis_labels
    port {
      name        = "redis"
      port        = 6379
      target_port = "redis"
      protocol    = "TCP"
    }
    port {
      name        = "sentinel"
      port        = 26379
      target_port = "sentinel"
      protocol    = "TCP"
    }
  }
}

# Bootstrap queries use only Ready Sentinel endpoints. RLS uses each stable Pod
# DNS name directly, so this Service never load-balances Redis writes.
resource "kubernetes_service_v1" "edge_rate_limit_redis_sentinel" {
  metadata {
    name      = local.edge_rate_limit_redis_sentinel_name
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    selector = local.edge_rate_limit_redis_labels
    port {
      name        = "sentinel"
      port        = 26379
      target_port = "sentinel"
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
    min_available = "2"
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
      ports {
        port     = "26379"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = local.edge_rate_limit_redis_labels
        }
      }
      ports {
        port     = "6379"
        protocol = "TCP"
      }
      ports {
        port     = "26379"
        protocol = "TCP"
      }
    }

    egress {
      to {
        pod_selector {
          match_labels = local.edge_rate_limit_redis_labels
        }
      }
      ports {
        port     = "6379"
        protocol = "TCP"
      }
      ports {
        port     = "26379"
        protocol = "TCP"
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
        pod_selector {
          match_labels = {
            "k8s-app" = "kube-dns"
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
  }
}
