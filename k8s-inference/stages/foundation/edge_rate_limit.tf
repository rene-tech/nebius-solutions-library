locals {
  edge_rate_limit_redis_name          = "fs2-edge-rate-limit-redis"
  edge_rate_limit_redis_headless_name = "${local.edge_rate_limit_redis_name}-headless"
  edge_rate_limit_redis_sentinel_name = "${local.edge_rate_limit_redis_name}-sentinel"
  edge_rate_limit_redis_master_name   = "fs2-edge-rate-limit"
  edge_rate_limit_redis_server_tls_secret_name = var.public_edge_redis_tls_handoff.server_secret_name
  edge_rate_limit_redis_client_tls_secret_name = var.public_edge_redis_tls_handoff.client_secret_name
  edge_rate_limit_redis_tls_handoff_sha256     = sha256(var.public_edge_redis_tls_handoff.envelope_json)
  edge_rate_limit_redis_cli = local.public_edge_enabled ? "redis-cli --tls --cacert /redis-server-tls/ca.crt --cert /redis-server-tls/tls.crt --key /redis-server-tls/tls.key" : "redis-cli"
  edge_rate_limit_redis_server_dns_names = sort(concat([
    "${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local",
    "${local.edge_rate_limit_redis_sentinel_name}.envoy-gateway-system.svc.cluster.local",
  ], [for ordinal in range(3) : "${local.edge_rate_limit_redis_name}-${ordinal}.${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"]))
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
    "kubernetes_pod_disruption_budget_v1.edge_rate_limit_service",
    "kubernetes_network_policy_v1.edge_rate_limit_redis",
    "kubernetes_network_policy_v1.edge_rate_limit_redis_default_deny",
    "kubernetes_network_policy_v1.edge_rate_limit_service",
    "kubernetes_network_policy_v1.edge_rate_limit_service_default_deny",
    "kubernetes_network_policy_v1.edge_gateway_controller_xds",
  ], local.public_edge_enabled ? [
    "terraform_data.public_edge_apply_eligibility[0]",
    "kubernetes_manifest.public_edge_node_authority_cas_policy[0]",
    "kubernetes_manifest.public_edge_node_authority_cas_binding[0]",
    "kubernetes_manifest.public_edge_node_authority_policy[0]",
    "kubernetes_manifest.public_edge_node_authority_binding[0]",
  ] : [])
}

# The rate-limit store is an ephemeral three-member Redis replication group
# supervised by three co-located Sentinels. Sentinels expose one logical write
# authority and require quorum before failover; the RLS connects to stable
# Sentinel endpoints instead of load-balancing writes across Redis Pods.
# Every Redis process asks all three stable Sentinel identities and requires two
# identical replies before it starts.  The ClusterIP service is never a source
# of authority.  A missing/stale/minority reply leaves Redis unstarted, while
# the concurrently started Sentinels retain one deterministic genesis target
# and converge through their quorum protocol.
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
      bootstrap_master="${local.edge_rate_limit_redis_name}-0.$headless_service"
      self_address="$HOSTNAME.$headless_service"

      mkdir -p /work/sentinel /data
      printf '%s\n' 'bind 0.0.0.0' 'protected-mode no' > /work/sentinel.conf
      if [ "${local.public_edge_enabled}" = "true" ]; then
        printf '%s\n' \
          'port 0' \
          'tls-port 26379' \
          'tls-cert-file /redis-server-tls/tls.crt' \
          'tls-key-file /redis-server-tls/tls.key' \
          'tls-ca-cert-file /redis-server-tls/ca.crt' \
          'tls-auth-clients yes' \
          'tls-replication yes' >> /work/sentinel.conf
      else
        printf '%s\n' 'port 26379' >> /work/sentinel.conf
      fi
      printf '%s\n' \
        'dir /work/sentinel' \
        'sentinel resolve-hostnames yes' \
        'sentinel announce-hostnames yes' \
        "sentinel announce-ip $self_address" \
        'sentinel announce-port 26379' \
        "sentinel monitor $master_name $bootstrap_master 6379 2" \
        "sentinel down-after-milliseconds $master_name 5000" \
        "sentinel failover-timeout $master_name 15000" \
        "sentinel parallel-syncs $master_name 1" >> /work/sentinel.conf
    EOT
    "run-redis.sh" = <<-EOT
      #!/bin/sh
      set -eu

      master_name="${local.edge_rate_limit_redis_master_name}"
      headless_service="${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"
      self_address="$HOSTNAME.$headless_service"
      redis_cli="${local.edge_rate_limit_redis_cli}"

      while :; do
        candidate=""
        candidate_count=0
        for ordinal in 0 1 2; do
          sentinel="${local.edge_rate_limit_redis_name}-$ordinal.$headless_service"
          reply="$($redis_cli -h "$sentinel" -p 26379 --raw SENTINEL get-master-addr-by-name "$master_name" 2>/dev/null || true)"
          address="$(printf '%s\n' "$reply" | sed -n '1p')"
          port="$(printf '%s\n' "$reply" | sed -n '2p')"
          case "$address" in
            "${local.edge_rate_limit_redis_name}-0.$headless_service"|"${local.edge_rate_limit_redis_name}-1.$headless_service"|"${local.edge_rate_limit_redis_name}-2.$headless_service") ;;
            *) continue ;;
          esac
          [ "$port" = "6379" ] || continue
          if [ -z "$candidate" ] || [ "$candidate" = "$address" ]; then
            candidate="$address"
            candidate_count=$((candidate_count + 1))
          else
            # A differing first reply cannot be used as an authority. Recount
            # each exact candidate so two agreeing Sentinels still win.
            for exact_candidate in \
              "${local.edge_rate_limit_redis_name}-0.$headless_service" \
              "${local.edge_rate_limit_redis_name}-1.$headless_service" \
              "${local.edge_rate_limit_redis_name}-2.$headless_service"; do
              count=0
              for check_ordinal in 0 1 2; do
                check_sentinel="${local.edge_rate_limit_redis_name}-$check_ordinal.$headless_service"
                check_reply="$($redis_cli -h "$check_sentinel" -p 26379 --raw SENTINEL get-master-addr-by-name "$master_name" 2>/dev/null || true)"
                [ "$(printf '%s\n' "$check_reply" | sed -n '1p')" = "$exact_candidate" ] && \
                  [ "$(printf '%s\n' "$check_reply" | sed -n '2p')" = "6379" ] && count=$((count + 1))
              done
              if [ "$count" -ge 2 ]; then
                candidate="$exact_candidate"
                candidate_count="$count"
                break
              fi
            done
            break
          fi
        done
        [ "$candidate_count" -ge 2 ] && break
        candidate=""
        sleep 1
      done

      printf '%s\n' 'bind 0.0.0.0' 'protected-mode no' > /work/redis.conf
      if [ "${local.public_edge_enabled}" = "true" ]; then
        printf '%s\n' \
          'port 0' \
          'tls-port 6379' \
          'tls-cert-file /redis-server-tls/tls.crt' \
          'tls-key-file /redis-server-tls/tls.key' \
          'tls-ca-cert-file /redis-server-tls/ca.crt' \
          'tls-auth-clients yes' \
          'tls-replication yes' >> /work/redis.conf
      else
        printf '%s\n' 'port 6379' >> /work/redis.conf
      fi
      printf '%s\n' \
        'dir /data' \
        'save ""' \
        'appendonly no' \
        'maxmemory 192mb' \
        'maxmemory-policy allkeys-lru' \
        'min-replicas-to-write 1' \
        'min-replicas-max-lag 5' \
        'replica-read-only yes' \
        "replica-announce-ip $self_address" \
        'replica-announce-port 6379' >> /work/redis.conf
      if [ "$self_address" != "$candidate" ]; then
        printf 'replicaof %s 6379\n' "$candidate" >> /work/redis.conf
      fi
      exec redis-server /work/redis.conf
    EOT
    "ready.sh" = <<-EOT
      #!/bin/sh
      set -eu
      master_name="${local.edge_rate_limit_redis_master_name}"
      headless_service="${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"
      self_address="$HOSTNAME.$headless_service"
      redis_cli="${local.edge_rate_limit_redis_cli}"
      agreed=""
      for exact_candidate in \
        "${local.edge_rate_limit_redis_name}-0.$headless_service" \
        "${local.edge_rate_limit_redis_name}-1.$headless_service" \
        "${local.edge_rate_limit_redis_name}-2.$headless_service"; do
        count=0
        for ordinal in 0 1 2; do
          sentinel="${local.edge_rate_limit_redis_name}-$ordinal.$headless_service"
          reply="$($redis_cli -h "$sentinel" -p 26379 --raw SENTINEL get-master-addr-by-name "$master_name" 2>/dev/null || true)"
          [ "$(printf '%s\n' "$reply" | sed -n '1p')" = "$exact_candidate" ] && \
            [ "$(printf '%s\n' "$reply" | sed -n '2p')" = "6379" ] && count=$((count + 1))
        done
        [ "$count" -ge 2 ] && agreed="$exact_candidate"
      done
      [ -n "$agreed" ]
      role="$($redis_cli -h "$self_address" --raw ROLE)"
      if [ "$agreed" = "$self_address" ]; then
        [ "$(printf '%s\n' "$role" | sed -n '1p')" = "master" ]
        # A primary is ready only while at least one current replica can
        # acknowledge writes inside the same five-second fence enforced by
        # Redis. In a 2/1 partition the isolated former primary therefore
        # becomes read-only instead of serving a second counter authority.
        replication="$($redis_cli -h "$self_address" --raw INFO replication)"
        connected_replicas="$(printf '%s\n' "$replication" | sed -n 's/^connected_slaves:\([0-9][0-9]*\)\r*$/\1/p')"
        [ -n "$connected_replicas" ]
        [ "$connected_replicas" -ge 1 ]
      else
        [ "$(printf '%s\n' "$role" | sed -n '1p')" = "slave" ]
        [ "$(printf '%s\n' "$role" | sed -n '2p')" = "$agreed" ]
        [ "$(printf '%s\n' "$role" | sed -n '3p')" = "6379" ]
        [ "$(printf '%s\n' "$role" | sed -n '4p')" = "connected" ]
      fi
    EOT
    "ping-redis.sh" = <<-EOT
      #!/bin/sh
      set -eu
      host="$HOSTNAME.${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"
      exec ${local.edge_rate_limit_redis_cli} -h "$host" ping
    EOT
    "ping-sentinel.sh" = <<-EOT
      #!/bin/sh
      set -eu
      host="$HOSTNAME.${local.edge_rate_limit_redis_headless_name}.envoy-gateway-system.svc.cluster.local"
      exec ${local.edge_rate_limit_redis_cli} -h "$host" -p 26379 ping
    EOT
    "tls-handoff-envelope.json" = var.public_edge_redis_tls_handoff.envelope_json
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
    pod_management_policy = "Parallel"

    selector {
      match_labels = local.edge_rate_limit_redis_labels
    }

    update_strategy {
      type = "RollingUpdate"
    }

    template {
      metadata {
        labels = local.edge_rate_limit_redis_labels
        annotations = {
          "fs2.nebius.ai/redis-tls-handoff-sha256" = local.edge_rate_limit_redis_tls_handoff_sha256
        }
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
          command           = ["/bin/sh", "/bootstrap/run-redis.sh"]

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
              command = ["/bin/sh", "/bootstrap/ready.sh"]
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          liveness_probe {
            exec {
              command = ["/bin/sh", "/bootstrap/ping-redis.sh"]
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 2
            failure_threshold     = 3
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
          dynamic "volume_mount" {
            for_each = local.public_edge_enabled ? [1] : []
            content {
              name       = "redis-server-tls"
              mount_path = "/redis-server-tls"
              read_only  = true
            }
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
              command = ["/bin/sh", "/bootstrap/ping-sentinel.sh"]
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          liveness_probe {
            exec {
              command = ["/bin/sh", "/bootstrap/ping-sentinel.sh"]
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
            name       = "bootstrap"
            mount_path = "/bootstrap"
            read_only  = true
          }
          dynamic "volume_mount" {
            for_each = local.public_edge_enabled ? [1] : []
            content {
              name       = "redis-server-tls"
              mount_path = "/redis-server-tls"
              read_only  = true
            }
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
        dynamic "volume" {
          for_each = local.public_edge_enabled ? [1] : []
          content {
            name = "redis-server-tls"
            secret {
              secret_name  = local.edge_rate_limit_redis_server_tls_secret_name
              default_mode = 288
              items {
                key  = "ca.crt"
                path = "ca.crt"
              }
              items {
                key  = "tls.crt"
                path = "tls.crt"
              }
              items {
                key  = "tls.key"
                path = "tls.key"
              }
            }
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
          data.external.public_edge_mutation_fence[0].result.admission_cas_policy_sha256 == local.public_edge_node_authority_cas_policy_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_cas_binding_sha256 == local.public_edge_node_authority_cas_binding_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_bootstrap_policy_sha256 == local.public_edge_cas_bootstrap_policy_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_bootstrap_binding_sha256 == local.public_edge_cas_bootstrap_binding_sha256 &&
          data.external.public_edge_mutation_fence[0].result.admission_boundary_approval_sha256 == local.public_edge_node_authority_approval_sha256 &&
          data.external.public_edge_mutation_fence[0].result.preventive_boundary_receipt_sha256 == local.public_edge_observed_preventive_boundary.receipt_sha256 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.provider_member_count) >= 3 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.eligible_node_count) >= 3 &&
          tonumber(data.external.public_edge_mutation_fence[0].result.hostname_domain_count) >= 3,
          false,
        )
      )
      error_message = "The public edge Redis/Sentinel StatefulSet requires the source-trusted provider membership receipt, continuous Node authority policy, and a fresh mutation fence with three eligible hostname domains."
    }
    precondition {
      condition = !local.public_edge_enabled || try(
        var.public_edge_redis_tls_handoff.schema == "fs2-serve.nebius.ai/edge-rate-limit-redis-tls-handoff/v1" &&
        var.public_edge_redis_tls_handoff.status == "enrolled" &&
        var.public_edge_redis_tls_handoff.issuer_group == "cert-manager.io" &&
        contains(["Issuer", "ClusterIssuer"], var.public_edge_redis_tls_handoff.issuer_kind) &&
        length(trimspace(var.public_edge_redis_tls_handoff.issuer_name)) > 0 &&
        can(regex("^[a-f0-9]{64}$", var.public_edge_redis_tls_handoff.ca_root_spki_sha256)) &&
        var.public_edge_redis_tls_handoff.server_secret_name == "fs2-edge-rate-limit-redis-server-tls" &&
        var.public_edge_redis_tls_handoff.client_secret_name == "fs2-edge-rate-limit-redis-client-tls" &&
        var.public_edge_redis_tls_handoff.server_secret_name != var.public_edge_redis_tls_handoff.client_secret_name &&
        var.public_edge_redis_tls_handoff.server_dns_names == local.edge_rate_limit_redis_server_dns_names &&
        sort(var.public_edge_redis_tls_handoff.server_extended_key_usage) == ["client auth", "server auth"] &&
        var.public_edge_redis_tls_handoff.client_extended_key_usage == ["client auth"] &&
        var.public_edge_redis_tls_handoff.client_spiffe_uri == "spiffe://fs2.nebius.ai/edge-rate-limit/client" &&
        var.public_edge_redis_tls_handoff.maximum_lifetime_seconds == 604800 &&
        var.public_edge_redis_tls_handoff.minimum_remaining_seconds == 86400 &&
        var.public_edge_redis_tls_handoff.generation > 0 &&
        can(timecmp(var.public_edge_redis_tls_handoff.issued_at, var.public_edge_redis_tls_handoff.expires_at)) &&
        timecmp(var.public_edge_redis_tls_handoff.issued_at, var.public_edge_redis_tls_handoff.expires_at) < 0 &&
        timecmp(var.public_edge_redis_tls_handoff.issued_at, timestamp()) <= 0 &&
        timecmp(timestamp(), var.public_edge_redis_tls_handoff.expires_at) < 0 &&
        can(regex("^[a-f0-9]{64}$", var.public_edge_redis_tls_handoff.predecessor_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.public_edge_redis_tls_handoff.evidence_sha256)) &&
        length(var.public_edge_redis_tls_handoff.envelope_json) >= 256 &&
        length(var.public_edge_redis_tls_handoff.envelope_json) <= 65536 &&
        sha256(var.public_edge_redis_tls_handoff.envelope_json) == var.public_edge_redis_tls_handoff.evidence_sha256,
        false,
      )
      error_message = "Public edge Redis requires a current security-owner TLS handoff with distinct server/replication and client identities, exact SAN/EKU/CA/rotation evidence, and no private material in Terraform."
    }
  }

  depends_on = [
    data.external.public_edge_mutation_fence,
    terraform_data.public_edge_apply_eligibility,
    kubernetes_service_v1.edge_rate_limit_redis_headless,
    kubernetes_service_v1.edge_rate_limit_redis_sentinel,
    kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis,
    kubernetes_network_policy_v1.edge_rate_limit_redis,
    kubernetes_network_policy_v1.edge_rate_limit_redis_default_deny,
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

# Envoy Gateway's fail-closed global RLS is on both public listeners. Protect
# at least one of its two replicas during every voluntary drain/upgrade; the
# exact label is injected into the generated Deployment by the pinned chart
# values and is also used by its hard spread/anti-affinity contract.
resource "kubernetes_pod_disruption_budget_v1" "edge_rate_limit_service" {
  metadata {
    name      = "fs2-edge-rate-limit-service"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = merge(local.common_labels, local.edge_rate_limit_service_labels)
  }

  spec {
    min_available = "1"
    selector {
      match_labels = local.edge_rate_limit_service_labels
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

# Isolation is an additive union in Kubernetes. Keep the empty-rule policy as
# the explicit default-deny member and the policy above as the only reviewed
# TLS Redis/Sentinel allowlist; neither object is treated as a replacement for
# the other by the semantic authority.
resource "kubernetes_network_policy_v1" "edge_rate_limit_redis_default_deny" {
  metadata {
    name      = "${local.edge_rate_limit_redis_name}-default-deny"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = local.edge_rate_limit_redis_labels
  }

  spec {
    pod_selector {
      match_labels = local.edge_rate_limit_redis_labels
    }
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "edge_rate_limit_service_default_deny" {
  metadata {
    name      = "fs2-edge-rate-limit-service-default-deny"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = merge(local.common_labels, local.edge_rate_limit_service_labels)
  }

  spec {
    pod_selector {
      match_labels = local.edge_rate_limit_service_labels
    }
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "edge_rate_limit_service" {
  metadata {
    name      = "fs2-edge-rate-limit-service"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = merge(local.common_labels, local.edge_rate_limit_service_labels)
  }

  spec {
    pod_selector {
      match_labels = local.edge_rate_limit_service_labels
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = local.edge_gateway_proxy_namespace
          }
        }
        pod_selector {
          match_labels = local.edge_gateway_proxy_labels
        }
      }
      ports {
        port     = "8081"
        protocol = "TCP"
      }
    }

    # Envoy calls the RLS on 8081. Preserve the separately required Prometheus
    # telemetry scrape without broadening the reviewed ingress contract.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-observability"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "prometheus"
          }
        }
      }
      ports {
        port     = "19001"
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

    # Envoy Gateway v1.8.3 configures the RLS with GRPC_XDS_SOTW and the
    # controller endpoint envoy-gateway:18001. Keep that required control
    # channel scoped to the one controller selector in this namespace.
    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "envoy-gateway-system"
          }
        }
        pod_selector {
          match_labels = local.edge_gateway_controller_labels
        }
      }
      ports {
        port     = "18001"
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

# Install controller ingress isolation before the Envoy Gateway Helm release
# creates its controller Pods. The run-scoped Helm instance label prevents a
# retained controller from another disposable lifecycle from sharing either
# xDS lane. Egress remains unisolated here so the pinned controller can retain
# its separately reviewed Kubernetes/DNS control-plane dependencies.
resource "kubernetes_network_policy_v1" "edge_gateway_controller_xds" {
  metadata {
    name      = "fs2-envoy-gateway-controller-xds"
    namespace = kubernetes_namespace_v1.platform["envoy-gateway-system"].metadata[0].name
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "envoy-controller-xds" })
  }

  spec {
    pod_selector {
      match_labels = local.edge_gateway_controller_labels
    }
    policy_types = ["Ingress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = local.edge_gateway_proxy_namespace
          }
        }
        pod_selector {
          match_labels = local.edge_gateway_proxy_labels
        }
      }
      ports {
        port     = "18000"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "envoy-gateway-system"
          }
        }
        pod_selector {
          match_labels = local.edge_rate_limit_service_labels
        }
      }
      ports {
        port     = "18001"
        protocol = "TCP"
      }
    }
  }
}
