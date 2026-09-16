locals {
  control_plane_network_policy_release_name    = "fs2-serve-control-plane"
  control_plane_network_policy_service_account = "fs2-network-policy-transition"
  control_plane_network_policy_state_name      = "fs2-network-policy-transition"
  control_plane_network_policy_gateway_namespace = try(
    local.control_plane_overrides.networkPolicy.gateway.namespaceLabels["kubernetes.io/metadata.name"],
    yamldecode(file("${local.control_plane_chart_root}/values.yaml")).networkPolicy.gateway.namespaceLabels["kubernetes.io/metadata.name"],
  )
  control_plane_network_policy_controller_namespace = try(
    local.control_plane_overrides.networkPolicy.envoyController.namespaceLabels["kubernetes.io/metadata.name"],
    yamldecode(file("${local.control_plane_chart_root}/values.yaml")).networkPolicy.envoyController.namespaceLabels["kubernetes.io/metadata.name"],
  )
  control_plane_network_policy_names = {
    proxy_normal      = "${local.control_plane_network_policy_release_name}-public-envoy"
    proxy_guard       = "${local.control_plane_network_policy_release_name}-public-envoy-transition-guard"
    controller_normal = "${local.control_plane_network_policy_release_name}-envoy-controller-xds"
    controller_guard  = "${local.control_plane_network_policy_release_name}-envoy-controller-xds-transition-guard"
    default_deny      = "${local.control_plane_network_policy_release_name}-envoy-default-deny"
  }
  control_plane_network_policy_boundary_labels = {
    "app.kubernetes.io/instance"            = local.control_plane_network_policy_release_name
    "app.kubernetes.io/managed-by"          = "terraform"
    "app.kubernetes.io/part-of"             = "fs2-serve"
    "fs2.nebius.ai/network-policy-boundary" = "permanent"
  }
}

resource "kubernetes_service_account_v1" "control_plane_network_policy_transition" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_service_account
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  automount_service_account_token = false

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "control_plane_network_policy_transition_receipt" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_state_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  data = {
    "receipt.json" = jsonencode({
      schema = "fs2-serve.nebius.ai/network-policy-transition-receipt/v2"
      phase  = "uninitialized"
    })
  }

  lifecycle {
    ignore_changes = [data]
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "control_plane_network_policy_transition_lease" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = {
    apiVersion = "coordination.k8s.io/v1"
    kind       = "Lease"
    metadata = {
      name      = local.control_plane_network_policy_state_name
      namespace = "fs2-system"
      labels    = local.control_plane_network_policy_boundary_labels
    }
    spec = {
      holderIdentity       = ""
      leaseDurationSeconds = 3600
    }
  }

  lifecycle {
    ignore_changes = [manifest.spec]
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_network_policy_v1" "control_plane_public_envoy_boundary" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_names.proxy_guard
    namespace = local.control_plane_network_policy_gateway_namespace
    labels = merge(local.control_plane_network_policy_boundary_labels, {
      "fs2.nebius.ai/network-policy-role" = "public-envoy"
    })
  }
  spec {
    pod_selector {
      match_labels = { "fs2.nebius.ai/network-policy-uninitialized" = "true" }
    }
    policy_types = ["Ingress", "Egress"]
  }

  lifecycle {
    ignore_changes = [metadata[0].annotations, spec]
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_network_policy_v1" "control_plane_envoy_controller_boundary" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_names.controller_guard
    namespace = local.control_plane_network_policy_controller_namespace
    labels = merge(local.control_plane_network_policy_boundary_labels, {
      "fs2.nebius.ai/network-policy-role" = "envoy-controller"
    })
  }
  spec {
    pod_selector {
      match_labels = { "fs2.nebius.ai/network-policy-uninitialized" = "true" }
    }
    policy_types = ["Ingress"]
  }

  lifecycle {
    ignore_changes = [metadata[0].annotations, spec]
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_network_policy_v1" "control_plane_envoy_default_deny" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_names.default_deny
    namespace = local.control_plane_network_policy_gateway_namespace
    labels = merge(local.control_plane_network_policy_boundary_labels, {
      "fs2.nebius.ai/network-policy-role" = "default-deny"
    })
  }
  spec {
    pod_selector {
      match_labels = { "fs2.nebius.ai/network-policy-deny-relaxed" = "true" }
    }
    policy_types = ["Ingress"]
  }

  lifecycle {
    ignore_changes = [metadata[0].annotations, spec]
  }

  # Creation is allows-first; Terraform reverses this edge on destroy so the
  # deny is always removed before either permanent allow, even after a partial
  # apply where the completion destroy provisioner was never created.
  depends_on = [
    kubernetes_network_policy_v1.control_plane_public_envoy_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_controller_boundary,
  ]
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_state" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_state_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups     = ["coordination.k8s.io"]
    resources      = ["leases"]
    resource_names = [local.control_plane_network_policy_state_name]
    verbs          = ["get", "patch", "update"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = [local.control_plane_network_policy_state_name]
    verbs          = ["get", "patch", "update"]
  }
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_state" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = local.control_plane_network_policy_state_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_state[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition[0].metadata[0].name
    namespace = "fs2-system"
  }
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_gateway" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-gateway"
    namespace = local.control_plane_network_policy_gateway_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    resource_names = [
      local.control_plane_network_policy_names.proxy_normal,
      local.control_plane_network_policy_names.proxy_guard,
      local.control_plane_network_policy_names.default_deny,
    ]
    verbs = ["get"]
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    resource_names = [
      local.control_plane_network_policy_names.proxy_guard,
      local.control_plane_network_policy_names.default_deny,
    ]
    verbs = ["patch", "update"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_gateway" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-gateway"
    namespace = local.control_plane_network_policy_gateway_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_gateway[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition[0].metadata[0].name
    namespace = "fs2-system"
  }
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_controller" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-controller"
    namespace = local.control_plane_network_policy_controller_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    resource_names = [
      local.control_plane_network_policy_names.controller_normal,
      local.control_plane_network_policy_names.controller_guard,
    ]
    verbs = ["get"]
  }
  rule {
    api_groups     = ["networking.k8s.io"]
    resources      = ["networkpolicies"]
    resource_names = [local.control_plane_network_policy_names.controller_guard]
    verbs          = ["patch", "update"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_controller" {
  count = local.public_edge_enabled ? 1 : 0

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-controller"
    namespace = local.control_plane_network_policy_controller_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_controller[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition[0].metadata[0].name
    namespace = "fs2-system"
  }
}

resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-network-policy-boundary"
      labels = local.control_plane_network_policy_boundary_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = ["networking.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["networkpolicies"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name       = "permanent-boundary"
        expression = "has(oldObject.metadata.labels) && oldObject.metadata.labels['fs2.nebius.ai/network-policy-boundary'] == 'permanent'"
      }]
      validations = [{
        expression = "request.userInfo.username == 'system:serviceaccount:fs2-system:${local.control_plane_network_policy_service_account}'"
        message    = "permanent NetworkPolicy boundaries may only be changed by the transition ServiceAccount"
        reason     = "Forbidden"
      }]
    }
  }

  depends_on = [
    kubernetes_network_policy_v1.control_plane_public_envoy_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_controller_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_default_deny,
  ]
}

resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission_binding" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-network-policy-boundary"
      labels = local.control_plane_network_policy_boundary_labels
    }
    spec = {
      policyName        = "fs2-network-policy-boundary"
      validationActions = ["Deny"]
    }
  }

  depends_on = [
    kubernetes_manifest.control_plane_network_policy_boundary_admission,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller,
    kubernetes_config_map_v1.control_plane_network_policy_transition_receipt,
    kubernetes_manifest.control_plane_network_policy_transition_lease,
  ]
}
