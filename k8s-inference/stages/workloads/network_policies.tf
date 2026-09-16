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
  model_namespace_support_profiles = {
    "acceptance-zero-egress-v1"     = { egress_mode = "none" }
    "cache-resident-zero-egress-v1" = { egress_mode = "none" }
    "job-internal-v1"               = { egress_mode = "internal" }
    "job-public-acquisition-v1"     = { egress_mode = "public-acquisition" }
  }
  model_runtime_profile_names = sort(distinct(concat(
    keys(local.model_runtime_base_profiles),
    keys(local.model_runtime_modelexpress_profiles),
    keys(local.model_namespace_support_profiles),
  )))
  model_runtime_allow_policy_names = [
    for profile in local.model_runtime_profile_names : "fs2-runtime-profile-${profile}"
  ]
  model_runtime_profiles_sha256 = sha256(jsonencode(local.model_runtime_profile_names))
  model_runtime_profile_admission_policy_names = [
    "fs2-model-network-profile-apps",
    "fs2-model-network-profile-jobs",
    "fs2-model-network-profile-jobsets",
    "fs2-model-network-profile-pods",
  ]
  model_runtime_boundary_marker_admission_policy_name = "fs2-model-network-boundary-marker"
  model_runtime_admission_policy_names = sort(concat(
    local.model_runtime_profile_admission_policy_names,
    [local.model_runtime_boundary_marker_admission_policy_name],
  ))
  model_runtime_admission_binding_names = [
    for name in local.model_runtime_admission_policy_names : "${name}-fs2-models"
  ]
  model_runtime_controller_deployment_name = "fs2-serve-control-plane-model-controller"
  model_runtime_admission_specs = {
    apps = {
      name         = "fs2-model-network-profile-apps"
      api_groups   = ["apps"]
      api_versions = ["v1"]
      resources    = ["deployments", "statefulsets", "daemonsets", "replicasets"]
      expression   = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_profile_names)} &&
        has(object.spec.template.metadata.labels) &&
        object.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    jobs = {
      name         = "fs2-model-network-profile-jobs"
      api_groups   = ["batch"]
      api_versions = ["v1"]
      resources    = ["jobs"]
      expression   = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_profile_names)} &&
        has(object.spec.template.metadata.labels) &&
        object.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    jobsets = {
      name         = "fs2-model-network-profile-jobsets"
      api_groups   = ["jobset.x-k8s.io"]
      api_versions = ["v1alpha2"]
      resources    = ["jobsets"]
      expression   = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_profile_names)} &&
        object.spec.replicatedJobs.size() > 0 &&
        object.spec.replicatedJobs.all(job,
          has(job.template.metadata.labels) &&
          job.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
          job.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
          has(job.template.spec.template.metadata.labels) &&
          job.template.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
          job.template.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}']) &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    pods = {
      name         = "fs2-model-network-profile-pods"
      api_groups   = [""]
      api_versions = ["v1"]
      resources    = ["pods"]
      expression   = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_profile_names)} &&
        has(object.metadata.ownerReferences) &&
        object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller == true) &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
  }

  model_runtime_inventory_receipt_payload = var.model_runtime_network_policy.inventory_receipt == null ? null : {
    schema             = var.model_runtime_network_policy.inventory_receipt.schema
    cluster_id         = var.model_runtime_network_policy.inventory_receipt.cluster_id
    namespace          = var.model_runtime_network_policy.inventory_receipt.namespace
    captured_at        = var.model_runtime_network_policy.inventory_receipt.captured_at
    profiles_sha256    = var.model_runtime_network_policy.inventory_receipt.profiles_sha256
    resource_apis      = var.model_runtime_network_policy.inventory_receipt.resource_apis
    workloads          = var.model_runtime_network_policy.inventory_receipt.workloads
    pods               = var.model_runtime_network_policy.inventory_receipt.pods
    live_controller    = var.model_runtime_network_policy.inventory_receipt.live_controller
    admission_bindings = var.model_runtime_network_policy.inventory_receipt.admission_bindings
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

  live_model_network_policy_names = var.model_runtime_network_policy.phase == "rollback-helm" ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_policy_prepare_names = contains(["prepare", "inventory"], var.model_runtime_network_policy.phase) ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_enforcement_markers = contains(["prepare", "rollback-helm"], var.model_runtime_network_policy.phase) ? coalesce(data.kubernetes_resources.model_runtime_network_enforcement_markers[0].objects, []) : []
  model_runtime_boundary_marker_payload = {
    schema                = "fs2-serve.nebius.ai/model-runtime-network-boundary/v2"
    profiles_sha256       = local.model_runtime_profiles_sha256
    admission_policies    = local.model_runtime_admission_policy_names
    admission_bindings    = local.model_runtime_admission_binding_names
    controller_deployment = local.model_runtime_controller_deployment_name
  }
}

data "kubernetes_resources" "model_runtime_network_policies" {
  count = contains([
    "prepare",
    "inventory",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  namespace   = "fs2-models"
}

data "kubernetes_resources" "model_runtime_network_enforcement_markers" {
  count = contains(["prepare", "rollback-helm"], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = "fs2-models"
  label_selector = "fs2-serve.nebius.ai/network-boundary-marker=true"
}

resource "terraform_data" "model_runtime_network_policy_transition" {
  input = {
    phase                        = var.model_runtime_network_policy.phase
    cluster_id                   = var.cluster_id
    namespace                    = "fs2-models"
    profiles                     = local.model_runtime_profile_names
    profiles_sha256              = local.model_runtime_profiles_sha256
    allow_policy_names           = local.model_runtime_allow_policy_names
    admission_policy_names       = local.model_runtime_admission_policy_names
    admission_binding_names      = local.model_runtime_admission_binding_names
    controller_deployment_name   = local.model_runtime_controller_deployment_name
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
      error_message = "Prepare is initial-only and requires both default-deny and the armed boundary marker to be absent. Once inventory arms admission, refresh receipts in enforce instead of returning to prepare."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "inventory" || (
        !contains(local.live_model_network_policy_prepare_names, "default-deny")
      )
      error_message = "Inventory can arm admission only while fs2-models/default-deny remains absent."
    }

    precondition {
      condition = !contains([
        "enforce",
        "rollback-remove-deny",
        "rollback-helm",
        ], var.model_runtime_network_policy.phase) || try(
        var.model_runtime_network_policy.inventory_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-inventory/v2" &&
        var.model_runtime_network_policy.inventory_receipt.cluster_id == var.cluster_id &&
        var.model_runtime_network_policy.inventory_receipt.namespace == "fs2-models" &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.model_runtime_network_policy.inventory_receipt.captured_at)) &&
        var.model_runtime_network_policy.inventory_receipt.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        length(var.model_runtime_network_policy.inventory_receipt.workloads) > 0 &&
        var.model_runtime_network_policy.inventory_receipt.live_controller.deployment_name == local.model_runtime_controller_deployment_name &&
        var.model_runtime_network_policy.inventory_receipt.live_controller.image == "${var.control_plane_image.repository}@${var.control_plane_image.digest}" &&
        jsonencode(sort(keys(var.model_runtime_network_policy.inventory_receipt.admission_bindings))) == jsonencode(local.model_runtime_admission_binding_names) &&
        alltrue([for uid in values(var.model_runtime_network_policy.inventory_receipt.admission_bindings) : uid != ""]) &&
        var.model_runtime_network_policy.inventory_receipt.payload_sha256 == sha256(jsonencode(local.model_runtime_inventory_receipt_payload)),
        false,
      )
      error_message = "Enforcement and deny removal require a valid v2 workload/Pod receipt for this cluster, the live digest-pinned model-controller, the admission binding UIDs, namespace, and finite profile catalog. Expected payload digest: ${sha256(jsonencode(local.model_runtime_inventory_receipt_payload))}."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "rollback-helm" || try(
        var.model_runtime_network_policy.deny_absent_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v2" &&
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
        local.live_model_network_enforcement_markers[0].metadata.name == "fs2-runtime-network-policy-boundary-v2" &&
        try(local.live_model_network_enforcement_markers[0].immutable, false) &&
        local.live_model_network_enforcement_markers[0].data.payload_sha256 == sha256(jsonencode(local.model_runtime_boundary_marker_payload)) &&
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

# This fence stays active in every transition phase. Once installed, no new
# Pod-producing object or naked Pod can enter fs2-models without selecting one
# of the finite Terraform-owned profiles. That closes the plan/apply race while
# still allowing arbitrary customer App UUIDs to reuse an immutable profile.
resource "kubernetes_manifest" "model_runtime_network_profile_admission" {
  for_each = local.model_runtime_admission_specs

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = each.value.name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-profile-admission"
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = each.value.api_groups
          apiVersions = each.value.api_versions
          operations  = ["CREATE", "UPDATE"]
          resources   = each.value.resources
          scope       = "Namespaced"
        }]
      }
      validations = [{
        expression = trimspace(each.value.expression)
        message    = "fs2-models workloads and Pods require one immutable Terraform-owned network profile"
        reason     = "Forbidden"
      }]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-profile-admission"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_manifest" "model_runtime_network_profile_admission_binding" {
  for_each = var.model_runtime_network_policy.phase == "prepare" ? {} : local.model_runtime_admission_specs

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${each.value.name}-fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-profile-admission"
      })
    }
    spec = {
      policyName        = each.value.name
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "kubernetes.io/metadata.name" = "fs2-models"
          }
        }
      }
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-profile-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model,
    kubernetes_manifest.cold_start_keeper,
    kubernetes_manifest.kueue_admission_acceptance,
    kubernetes_manifest.model_runtime_network_profile_admission,
  ]
}

# The model controller legitimately manages ordinary ConfigMaps for dynamic
# Apps, so its namespaced ConfigMap verbs cannot be removed. Protect the stable
# transition marker at admission instead: after inventory starts, nobody can
# update or delete the exact marker, while all other ConfigMaps keep their
# existing behavior. The controller has no cluster-scoped admission-policy
# permissions and therefore cannot weaken this fence.
resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_boundary_marker_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-boundary-marker-admission"
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["configmaps"]
          scope       = "Namespaced"
        }]
      }
      validations = [{
        expression = "oldObject.metadata.name != 'fs2-runtime-network-policy-boundary-v2'"
        message    = "the fs2 model-network boundary marker is immutable and non-deletable"
        reason     = "Forbidden"
      }]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-boundary-marker-admission"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission_binding" {
  count = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_boundary_marker_admission_policy_name}-fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-boundary-marker-admission"
      })
    }
    spec = {
      policyName        = local.model_runtime_boundary_marker_admission_policy_name
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "kubernetes.io/metadata.name" = "fs2-models"
          }
        }
      }
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-boundary-marker-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.model_runtime_network_boundary_marker_admission,
  ]
}

resource "kubernetes_config_map_v1" "model_runtime_network_enforcement" {
  count = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1

  metadata {
    name      = "fs2-runtime-network-policy-boundary-v2"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"                 = "namespace-network-boundary"
      "fs2-serve.nebius.ai/network-boundary-marker" = "true"
    })
  }

  data = {
    schema         = local.model_runtime_boundary_marker_payload.schema
    payload_sha256 = sha256(jsonencode(local.model_runtime_boundary_marker_payload))
  }

  immutable = true

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    terraform_data.model_runtime_network_policy_transition,
    kubernetes_manifest.model_runtime_network_profile_admission_binding,
    kubernetes_manifest.model_runtime_network_boundary_marker_admission_binding,
  ]
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

resource "kubernetes_network_policy_v1" "model_namespace_support_profile" {
  for_each = local.model_namespace_support_profiles

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
        "app.kubernetes.io/part-of"                 = "fs2-serve"
        (local.model_runtime_network_profile_label) = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    dynamic "egress" {
      for_each = contains(["internal", "public-acquisition"], each.value.egress_mode) ? [true] : []
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

    dynamic "egress" {
      for_each = each.value.egress_mode == "internal" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
          }
          pod_selector {
            match_labels = {
              "app.kubernetes.io/name"     = "fs2-serve-control-plane"
              "app.kubernetes.io/instance" = "fs2-serve-control-plane"
            }
          }
        }
        ports {
          protocol = "TCP"
          port     = "8080"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "internal" ? toset(var.scientific_artifacts.egress_cidrs) : toset([])
      content {
        to {
          ip_block {
            cidr = egress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "public-acquisition" ? [true] : []
      content {
        to {
          ip_block {
            cidr = "0.0.0.0/0"
            except = [
              "0.0.0.0/8",
              "10.0.0.0/8",
              "100.64.0.0/10",
              "127.0.0.0/8",
              "169.254.0.0/16",
              "172.16.0.0/12",
              "192.0.0.0/24",
              "192.0.2.0/24",
              "192.168.0.0/16",
              "198.18.0.0/15",
              "198.51.100.0/24",
              "203.0.113.0/24",
              "224.0.0.0/4",
              "240.0.0.0/4",
            ]
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "public-acquisition" ? [true] : []
      content {
        to {
          ip_block {
            cidr = "::/0"
            except = [
              "::/128",
              "::1/128",
              "2001:db8::/32",
              "fc00::/7",
              "fe80::/10",
              "ff00::/8",
            ]
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }
  }
}

# The receipt is re-read from the live API during apply, after every known
# producer and the admission bindings exist. It runs on the first enforcement
# and whenever the receipt, release image, profile catalog, or verifier changes.
resource "terraform_data" "model_runtime_network_policy_apply_fence" {
  count = var.model_runtime_network_policy.phase == "enforce" ? 1 : 0

  triggers_replace = [
    var.model_runtime_network_policy.inventory_receipt.payload_sha256,
    local.model_runtime_profiles_sha256,
    var.control_plane_image.digest,
    filesha256("${path.module}/scripts/model_network_policy_transition.py"),
  ]

  provisioner "local-exec" {
    command = "python3 ${jsonencode("${path.module}/scripts/model_network_policy_transition.py")} verify-enforce --contract-json-env FS2_NETWORK_TRANSITION_JSON --receipt-json-env FS2_NETWORK_RECEIPT_JSON --kubeconfig ${jsonencode(pathexpand(var.kubeconfig_path))} --context ${jsonencode(var.kube_context)}"
    environment = {
      FS2_NETWORK_TRANSITION_JSON = jsonencode(terraform_data.model_runtime_network_policy_transition.output)
      FS2_NETWORK_RECEIPT_JSON    = jsonencode(var.model_runtime_network_policy.inventory_receipt)
    }
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model,
    kubernetes_manifest.cold_start_keeper,
    kubernetes_manifest.kueue_admission_acceptance,
    kubernetes_manifest.model_runtime_network_profile_admission_binding,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
    kubernetes_network_policy_v1.model_namespace_support_profile,
  ]
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
    terraform_data.model_runtime_network_policy_apply_fence,
    kubernetes_config_map_v1.model_runtime_network_enforcement,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
    kubernetes_network_policy_v1.model_namespace_support_profile,
  ]
}
