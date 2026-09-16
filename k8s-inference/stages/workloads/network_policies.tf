locals {
  model_runtime_network_profile_label = "fs2-serve.nebius.ai/network-profile"

  # These catalog models start exclusively from exact mounted content and keep
  # a true zero-egress contract. They deliberately do not join a DNS profile:
  # NetworkPolicy allows are additive, so a second DNS policy would reopen it.
  model_runtime_zero_egress_model_ids = toset([
    "glm-5-2-fp8",
    "nv-reason-cxr-3b",
    "qwen3-8b",
  ])
  model_runtime_base_profile_rows = [
    for model_id, route in local.selected_routes : {
      profile = format(
        "gateway-%s-tcp-%d-v1",
        contains(local.model_runtime_zero_egress_model_ids, model_id) ? "zero-egress" : "dns",
        route.service.port,
      )
      service_port = route.service.port
      egress_mode = (
        contains(local.model_runtime_zero_egress_model_ids, model_id) ? "none" : "dns"
      )
    }
  ]
  model_runtime_base_profile_groups = {
    for row in local.model_runtime_base_profile_rows : row.profile => row...
  }
  model_runtime_base_profiles = {
    for profile, rows in local.model_runtime_base_profile_groups : profile => rows[0]
  }

  # ModelExpress profiles are finite and qualification-owned. Arbitrarily
  # named customer Apps reuse the exact profile for their canonical runtime,
  # accelerator class, transport, port and device count; no policy is created
  # from an App UUID. The digest mirrors model_deployment.py.
  model_runtime_modelexpress_profile_rows = flatten([
    for model_id, binding in local.model_controller_modelexpress_bindings : [
      for pool_id in binding.poolRefs : {
        profile = "mx-${substr(sha256(jsonencode({
          acceleratorClass       = local.selected_queue_pools[pool_id].accelerator_class
          acceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
          configDigest           = binding.configDigest
          nixlBackend            = binding.poolTransports[pool_id].nixlBackend
          servicePort            = local.selected_routes[model_id].service.port
        })), 0, 60)}"
        service_port             = local.selected_routes[model_id].service.port
        accelerators_per_replica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
        coordinator_port         = tonumber(element(split(":", binding.endpoint), 1))
        coordinator_type         = binding.coordinatorNetworkType
        coordinator_namespace    = binding.coordinatorNamespace
        coordinator_pod_labels   = binding.coordinatorPodLabels
        coordinator_cidrs        = binding.coordinatorCidrs
      }
    ]
  ])
  model_runtime_modelexpress_profile_groups = {
    for row in local.model_runtime_modelexpress_profile_rows : row.profile => row...
  }
  model_runtime_modelexpress_profiles = {
    for profile, rows in local.model_runtime_modelexpress_profile_groups : profile => rows[0]
  }
}

resource "kubernetes_network_policy_v1" "model_runtime_base_profile" {
  for_each = local.model_runtime_base_profiles

  metadata {
    name      = "fs2-runtime-profile-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"               = "model-runtime-network"
      (local.model_runtime_network_profile_label) = each.key
      "fs2-serve.nebius.ai/policy-owner"          = "terraform-profile"
    })
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "model-runtime"
        (local.model_runtime_network_profile_label) = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(each.value.service_port)
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "dns" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
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
          protocol = "UDP"
          port     = "53"
        }
        ports {
          protocol = "TCP"
          port     = "53"
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "model_runtime_modelexpress_profile" {
  for_each = local.model_runtime_modelexpress_profiles

  metadata {
    name      = "fs2-runtime-profile-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"               = "model-runtime-network"
      (local.model_runtime_network_profile_label) = each.key
      "fs2-serve.nebius.ai/policy-owner"          = "terraform-profile"
    })
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "model-runtime"
        (local.model_runtime_network_profile_label) = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(each.value.service_port)
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            (local.model_runtime_network_profile_label) = each.key
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = "5555"
        end_port = each.value.accelerators_per_replica > 1 ? 5555 + each.value.accelerators_per_replica - 1 : null
      }
      ports {
        protocol = "TCP"
        port     = "6555"
        end_port = each.value.accelerators_per_replica > 1 ? 6555 + each.value.accelerators_per_replica - 1 : null
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
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
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }

    egress {
      to {
        pod_selector {
          match_labels = {
            (local.model_runtime_network_profile_label) = each.key
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = "5555"
        end_port = each.value.accelerators_per_replica > 1 ? 5555 + each.value.accelerators_per_replica - 1 : null
      }
      ports {
        protocol = "TCP"
        port     = "6555"
        end_port = each.value.accelerators_per_replica > 1 ? 6555 + each.value.accelerators_per_replica - 1 : null
      }
    }

    dynamic "egress" {
      for_each = each.value.coordinator_type == "pod-selector" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = {
              "kubernetes.io/metadata.name" = each.value.coordinator_namespace
            }
          }
          pod_selector {
            match_labels = each.value.coordinator_pod_labels
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(each.value.coordinator_port)
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.coordinator_type == "ip-blocks" ? toset(each.value.coordinator_cidrs) : toset([])
      content {
        to {
          ip_block {
            cidr = egress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(each.value.coordinator_port)
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "model_namespace_default_deny" {
  metadata {
    name      = "default-deny"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "namespace-network-boundary"
    })
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  # Every finite allow profile exists before the namespace closes. Rollback
  # destroys the deny before removing either class of allow profile.
  depends_on = [
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
  ]
}
