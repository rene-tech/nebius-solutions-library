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
  model_runtime_profile_names = sort(distinct(concat(
    keys(local.model_runtime_base_profiles),
    keys(local.model_runtime_modelexpress_profiles),
  )))
  model_runtime_allow_policy_names = [
    for profile in local.model_runtime_profile_names : "fs2-runtime-profile-${profile}"
  ]
  model_runtime_profiles_sha256 = sha256(jsonencode(local.model_runtime_profile_names))

  model_runtime_inventory_receipt_payload = var.model_runtime_network_policy.inventory_receipt == null ? null : {
    schema              = var.model_runtime_network_policy.inventory_receipt.schema
    cluster_id          = var.model_runtime_network_policy.inventory_receipt.cluster_id
    namespace           = var.model_runtime_network_policy.inventory_receipt.namespace
    captured_at         = var.model_runtime_network_policy.inventory_receipt.captured_at
    control_plane_image = var.model_runtime_network_policy.inventory_receipt.control_plane_image
    profiles_sha256     = var.model_runtime_network_policy.inventory_receipt.profiles_sha256
    deployments         = var.model_runtime_network_policy.inventory_receipt.deployments
  }
  model_runtime_deny_absent_receipt_payload = var.model_runtime_network_policy.deny_absent_receipt == null ? null : {
    schema                     = var.model_runtime_network_policy.deny_absent_receipt.schema
    cluster_id                 = var.model_runtime_network_policy.deny_absent_receipt.cluster_id
    namespace                  = var.model_runtime_network_policy.deny_absent_receipt.namespace
    captured_at                = var.model_runtime_network_policy.deny_absent_receipt.captured_at
    enforcement_payload_sha256 = var.model_runtime_network_policy.deny_absent_receipt.enforcement_payload_sha256
    profiles_sha256            = var.model_runtime_network_policy.deny_absent_receipt.profiles_sha256
    allow_policy_names         = var.model_runtime_network_policy.deny_absent_receipt.allow_policy_names
    default_deny_absent        = var.model_runtime_network_policy.deny_absent_receipt.default_deny_absent
  }

  # Enforcement reads every Deployment in the model namespace, not only ones
  # already carrying the expected selector. This makes a missing component or
  # profile label fail closed instead of hiding the workload from inventory.
  live_model_runtime_deployments = var.model_runtime_network_policy.phase == "enforce" ? {
    for deployment in data.kubernetes_resources.model_runtime_deployments[0].objects :
    deployment.metadata.name => {
      uid                = try(deployment.metadata.uid, "")
      profile            = try(deployment.metadata.labels[local.model_runtime_network_profile_label], "")
      workload_component = try(deployment.metadata.labels["app.kubernetes.io/component"], "")
      workload_part_of   = try(deployment.metadata.labels["app.kubernetes.io/part-of"], "")
      pod_component      = try(deployment.spec.template.metadata.labels["app.kubernetes.io/component"], "")
      pod_part_of        = try(deployment.spec.template.metadata.labels["app.kubernetes.io/part-of"], "")
      pod_profile        = try(deployment.spec.template.metadata.labels[local.model_runtime_network_profile_label], "")
    }
  } : {}
  live_model_runtime_inventory = {
    for name, deployment in local.live_model_runtime_deployments : name => {
      uid                = deployment.uid
      profile            = deployment.profile
      workload_component = deployment.workload_component
      workload_part_of   = deployment.workload_part_of
      pod_component      = deployment.pod_component
      pod_part_of        = deployment.pod_part_of
    }
  }
  live_model_network_policy_names = var.model_runtime_network_policy.phase == "rollback-helm" ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_policy_prepare_names = var.model_runtime_network_policy.phase == "prepare" ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_enforcement_markers = contains([
    "prepare",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? coalesce(data.kubernetes_resources.model_runtime_network_enforcement_markers[0].objects, []) : []
}

data "kubernetes_resources" "model_runtime_deployments" {
  count = var.model_runtime_network_policy.phase == "enforce" ? 1 : 0

  api_version = "apps/v1"
  kind        = "Deployment"
  namespace   = "fs2-models"
}

data "kubernetes_resources" "model_runtime_network_policies" {
  count = contains([
    "prepare",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  namespace   = "fs2-models"
}

data "kubernetes_resources" "model_runtime_network_enforcement_markers" {
  count = contains([
    "prepare",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = "fs2-models"
  label_selector = "fs2-serve.nebius.ai/network-enforcement-marker=true"
}

resource "terraform_data" "model_runtime_network_policy_transition" {
  input = {
    phase                        = var.model_runtime_network_policy.phase
    cluster_id                   = var.cluster_id
    namespace                    = "fs2-models"
    profiles                     = local.model_runtime_profile_names
    profiles_sha256              = local.model_runtime_profiles_sha256
    allow_policy_names           = local.model_runtime_allow_policy_names
    control_plane_image          = var.control_plane_image
    inventory_receipt_sha256     = try(var.model_runtime_network_policy.inventory_receipt.payload_sha256, null)
    deny_absent_receipt_sha256   = try(var.model_runtime_network_policy.deny_absent_receipt.payload_sha256, null)
    default_deny_planned         = var.model_runtime_network_policy.phase == "enforce"
    helm_rollback_authorized     = var.model_runtime_network_policy.phase == "rollback-helm"
    deny_removal_apply_isolation = var.model_runtime_network_policy.phase == "rollback-remove-deny"
  }

  lifecycle {
    precondition {
      condition = var.model_runtime_network_policy.phase != "prepare" || (
        !contains(local.live_model_network_policy_prepare_names, "default-deny") &&
        length(local.live_model_network_enforcement_markers) == 0
      )
      error_message = "Prepare is initial-only. A live default-deny or enforcement marker requires rollback-remove-deny followed by the deny-absent receipt path; phase renaming cannot combine deny removal with Helm rollback."
    }

    precondition {
      condition = !contains([
        "enforce",
        "rollback-remove-deny",
        "rollback-helm",
        ], var.model_runtime_network_policy.phase) || try(
        var.model_runtime_network_policy.inventory_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-inventory/v1" &&
        var.model_runtime_network_policy.inventory_receipt.cluster_id == var.cluster_id &&
        var.model_runtime_network_policy.inventory_receipt.namespace == "fs2-models" &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.model_runtime_network_policy.inventory_receipt.captured_at)) &&
        var.model_runtime_network_policy.inventory_receipt.control_plane_image == var.control_plane_image &&
        var.model_runtime_network_policy.inventory_receipt.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        var.model_runtime_network_policy.inventory_receipt.payload_sha256 == sha256(jsonencode(local.model_runtime_inventory_receipt_payload)),
        false,
      )
      error_message = "Enforcement and deny removal require a valid inventory receipt for this cluster, exact control-plane image, namespace, and finite profile catalog."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "enforce" || try(
        length(local.live_model_runtime_inventory) > 0 &&
        jsonencode(local.live_model_runtime_inventory) == jsonencode(var.model_runtime_network_policy.inventory_receipt.deployments) &&
        alltrue([
          for deployment in values(local.live_model_runtime_deployments) :
          deployment.uid != "" &&
          deployment.workload_component == "model-runtime" &&
          deployment.workload_part_of == "fs2-serve" &&
          deployment.pod_component == "model-runtime" &&
          deployment.pod_part_of == "fs2-serve" &&
          deployment.profile == deployment.pod_profile &&
          contains(local.model_runtime_profile_names, deployment.profile)
        ]),
        false,
      )
      error_message = "Refusing fs2-models default-deny: the live Deployment inventory is empty, changed since its receipt, missing runtime labels, or names an unrecognized finite network profile. Live normalized inventory: ${jsonencode(local.live_model_runtime_inventory)}."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "rollback-helm" || try(
        var.model_runtime_network_policy.deny_absent_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v1" &&
        var.model_runtime_network_policy.deny_absent_receipt.cluster_id == var.cluster_id &&
        var.model_runtime_network_policy.deny_absent_receipt.namespace == "fs2-models" &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.model_runtime_network_policy.deny_absent_receipt.captured_at)) &&
        var.model_runtime_network_policy.deny_absent_receipt.enforcement_payload_sha256 == var.model_runtime_network_policy.inventory_receipt.payload_sha256 &&
        var.model_runtime_network_policy.deny_absent_receipt.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        jsonencode(var.model_runtime_network_policy.deny_absent_receipt.allow_policy_names) == jsonencode(local.model_runtime_allow_policy_names) &&
        var.model_runtime_network_policy.deny_absent_receipt.default_deny_absent &&
        var.model_runtime_network_policy.deny_absent_receipt.payload_sha256 == sha256(jsonencode(local.model_runtime_deny_absent_receipt_payload)) &&
        !contains(local.live_model_network_policy_names, "default-deny") &&
        length(local.live_model_network_enforcement_markers) == 1 &&
        local.live_model_network_enforcement_markers[0].metadata.name == "fs2-runtime-network-policy-enforcement" &&
        local.live_model_network_enforcement_markers[0].data.inventory_receipt_sha256 == var.model_runtime_network_policy.inventory_receipt.payload_sha256 &&
        local.live_model_network_enforcement_markers[0].data.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        local.live_model_network_enforcement_markers[0].data.control_plane_image_digest == var.model_runtime_network_policy.inventory_receipt.control_plane_image.digest &&
        length(setsubtract(
          toset(local.model_runtime_allow_policy_names),
          toset(local.live_model_network_policy_names),
        )) == 0,
        false,
      )
      error_message = "Helm rollback is forbidden until a separate applied rollback-remove-deny phase has a valid receipt and live fs2-models inventory proves default-deny absent while finite allow profiles remain. Expected receipt digest: ${sha256(jsonencode(local.model_runtime_deny_absent_receipt_payload))}; live policies: ${jsonencode(local.live_model_network_policy_names)}."
    }
  }
}

resource "kubernetes_config_map_v1" "model_runtime_network_enforcement" {
  count = contains([
    "enforce",
    "rollback-remove-deny",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  metadata {
    name      = "fs2-runtime-network-policy-enforcement"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"                    = "namespace-network-boundary"
      "fs2-serve.nebius.ai/network-enforcement-marker" = "true"
    })
  }

  data = {
    schema                     = "fs2-serve.nebius.ai/model-runtime-network-enforcement/v1"
    inventory_receipt_sha256   = try(var.model_runtime_network_policy.inventory_receipt.payload_sha256, "")
    profiles_sha256            = local.model_runtime_profiles_sha256
    control_plane_image_digest = try(var.model_runtime_network_policy.inventory_receipt.control_plane_image.digest, "")
  }

  depends_on = [terraform_data.model_runtime_network_policy_transition]
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
  count = var.model_runtime_network_policy.phase == "enforce" ? 1 : 0

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
    terraform_data.model_runtime_network_policy_transition,
    kubernetes_config_map_v1.model_runtime_network_enforcement,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
  ]
}
