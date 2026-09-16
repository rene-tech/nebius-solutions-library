// This boundary deliberately lives in the foundation state, outside the
// workload/Helm lifecycle it protects. External security-owner IAM protects
// the admission objects; normal foundation destroy and targeted replacement
// are refused as a second independent guard.
locals {
  control_plane_network_policy_release_name         = "fs2-serve-control-plane"
  control_plane_network_policy_service_account      = "fs2-network-policy-transition"
  control_plane_network_policy_security_owner       = var.network_policy_boundary.security_owner_username
  control_plane_network_policy_state_name           = "fs2-network-policy-transition"
  control_plane_network_policy_topology_name        = "fs2-network-policy-boundary-topology"
  control_plane_network_policy_gateway_namespace    = var.network_policy_boundary.gateway_namespace
  control_plane_network_policy_controller_namespace = var.network_policy_boundary.controller_namespace
  control_plane_network_policy_security_owner_kubeconfig_path = coalesce(
    var.network_policy_boundary.security_owner_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-security-owner-kubeconfig",
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

resource "terraform_data" "control_plane_network_policy_security_owner_preflight" {
  input = {
    ordinary_kubeconfig       = abspath(var.kubeconfig_path)
    security_owner_kubeconfig = abspath(local.control_plane_network_policy_security_owner_kubeconfig_path)
    kube_context              = var.kube_context
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      test -f "$FS2_SECURITY_KUBECONFIG"
      test "$(stat -c '%a' "$FS2_SECURITY_KUBECONFIG")" = "600"
      test "$FS2_ORDINARY_KUBECONFIG" != "$FS2_SECURITY_KUBECONFIG"
      test "$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | jq -r '.status.userInfo.username')" = "$FS2_SECURITY_OWNER_USERNAME"
      test "$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | jq -r '.status.userInfo.username')" != "$FS2_SECURITY_OWNER_USERNAME"
      ordinary_can() {
        kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      security_can() {
        kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      test "$(ordinary_can update validatingadmissionpolicies.admissionregistration.k8s.io)" = "no"
      test "$(ordinary_can delete validatingadmissionpolicies.admissionregistration.k8s.io)" = "no"
      test "$(ordinary_can update validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "no"
      test "$(ordinary_can delete validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "no"
      test "$(ordinary_can impersonate "users/$FS2_SECURITY_OWNER_USERNAME")" = "no"
      test "$(security_can create validatingadmissionpolicies.admissionregistration.k8s.io)" = "yes"
      test "$(security_can update validatingadmissionpolicies.admissionregistration.k8s.io)" = "yes"
      test "$(security_can delete validatingadmissionpolicies.admissionregistration.k8s.io)" = "yes"
      test "$(security_can create validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "yes"
      test "$(security_can update validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "yes"
      test "$(security_can delete validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "yes"
    EOT
    environment = {
      FS2_ORDINARY_KUBECONFIG     = self.input.ordinary_kubeconfig
      FS2_SECURITY_KUBECONFIG     = self.input.security_owner_kubeconfig
      FS2_KUBE_CONTEXT            = self.input.kube_context
      FS2_SECURITY_OWNER_USERNAME = local.control_plane_network_policy_security_owner
    }
  }

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_service_account_v1" "control_plane_network_policy_transition" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_service_account
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  automount_service_account_token = false

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_config_map_v1" "control_plane_network_policy_topology" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_topology_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  data = {
    "topology.json" = jsonencode({
      schema                  = "fs2-serve.nebius.ai/network-policy-boundary-topology/v1"
      mode                    = var.network_policy_boundary.mode
      security_owner_username = local.control_plane_network_policy_security_owner
      gateway_namespace       = local.control_plane_network_policy_gateway_namespace
      controller_namespace    = local.control_plane_network_policy_controller_namespace
      policy_names            = local.control_plane_network_policy_names
    })
  }

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_config_map_v1" "control_plane_network_policy_transition_receipt" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_state_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  data = {
    "receipt.json" = jsonencode({
      schema = "fs2-serve.nebius.ai/network-policy-transition-receipt/v3"
      phase  = "uninitialized"
    })
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [data]
  }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_manifest" "control_plane_network_policy_transition_lease" {
  provider = kubernetes.network_policy_security_owner

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
      leaseDurationSeconds = 60
      leaseTransitions     = 0
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [manifest.spec]
  }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_network_policy_v1" "control_plane_public_envoy_boundary" {
  provider = kubernetes.network_policy_security_owner

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
    prevent_destroy = true
    ignore_changes  = [metadata[0].annotations, spec]
  }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_network_policy_v1" "control_plane_envoy_controller_boundary" {
  provider = kubernetes.network_policy_security_owner

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
    prevent_destroy = true
    ignore_changes  = [metadata[0].annotations, spec]
  }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_network_policy_v1" "control_plane_envoy_default_deny" {
  provider = kubernetes.network_policy_security_owner

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
    prevent_destroy = true
    ignore_changes  = [metadata[0].annotations, spec]
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
  provider = kubernetes.network_policy_security_owner

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
    api_groups = [""]
    resources  = ["configmaps"]
    resource_names = [
      local.control_plane_network_policy_state_name,
      local.control_plane_network_policy_topology_name,
    ]
    verbs = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = [local.control_plane_network_policy_state_name]
    verbs          = ["get", "patch", "update"]
  }

  lifecycle { prevent_destroy = true }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_owner" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = local.control_plane_network_policy_security_owner
    labels = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups     = ["networking.k8s.io"]
    resources      = ["networkpolicies"]
    resource_names = values(local.control_plane_network_policy_names)
    verbs          = ["get", "patch", "update", "delete"]
  }
  rule {
    api_groups     = ["admissionregistration.k8s.io"]
    resources      = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
    resource_names = ["fs2-network-policy-boundary"]
    verbs          = ["get", "patch", "update", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    resource_names = distinct([
      "fs2-system",
      local.control_plane_network_policy_gateway_namespace,
      local.control_plane_network_policy_controller_namespace,
    ])
    verbs = ["get", "patch", "update", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["serviceaccounts", "configmaps"]
    resource_names = [
      local.control_plane_network_policy_service_account,
      local.control_plane_network_policy_state_name,
      local.control_plane_network_policy_topology_name,
    ]
    verbs = ["get", "patch", "update", "delete"]
  }
  rule {
    api_groups     = ["coordination.k8s.io"]
    resources      = ["leases"]
    resource_names = [local.control_plane_network_policy_state_name]
    verbs          = ["get", "patch", "update", "delete"]
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
    resource_names = [
      local.control_plane_network_policy_state_name,
      "${local.control_plane_network_policy_state_name}-gateway",
      "${local.control_plane_network_policy_state_name}-controller",
      local.control_plane_network_policy_security_owner,
    ]
    verbs = ["get", "patch", "update", "delete"]
  }

  lifecycle { prevent_destroy = true }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_binding_v1" "control_plane_network_policy_security_owner" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = local.control_plane_network_policy_security_owner
    labels = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.control_plane_network_policy_security_owner.metadata[0].name
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_owner
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_state" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_state_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_state.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition.metadata[0].name
    namespace = "fs2-system"
  }

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_gateway" {
  provider = kubernetes.network_policy_security_owner

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

  lifecycle { prevent_destroy = true }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_gateway" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-gateway"
    namespace = local.control_plane_network_policy_gateway_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_gateway.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition.metadata[0].name
    namespace = "fs2-system"
  }

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_controller" {
  provider = kubernetes.network_policy_security_owner

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

  lifecycle { prevent_destroy = true }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_controller" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = "${local.control_plane_network_policy_state_name}-controller"
    namespace = local.control_plane_network_policy_controller_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.control_plane_network_policy_transition_controller.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.control_plane_network_policy_transition.metadata[0].name
    namespace = "fs2-system"
  }

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission" {
  provider = kubernetes.network_policy_security_owner

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
          apiGroups   = ["*"]
          apiVersions = ["*"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["*"]
          scope       = "*"
        }]
      }
      matchConditions = [{
        name       = "permanent-boundary"
        expression = "has(oldObject.metadata.labels) && oldObject.metadata.labels['fs2.nebius.ai/network-policy-boundary'] == 'permanent'"
      }]
      validations = [
        {
          expression = <<-CEL
            request.operation != 'DELETE' || (
              request.userInfo.username == '${local.control_plane_network_policy_security_owner}' &&
              has(oldObject.metadata.annotations) &&
              'fs2.nebius.ai/decommission-receipt-sha256' in oldObject.metadata.annotations &&
              oldObject.metadata.annotations['fs2.nebius.ai/decommission-receipt-sha256'].matches('^[0-9a-f]{64}$')
            )
          CEL
          message    = "permanent boundary deletion requires the external security owner and a bound decommission receipt"
          reason     = "Forbidden"
        },
        {
          expression = <<-CEL
            request.operation != 'UPDATE' || (
              has(object.metadata.labels) &&
              object.metadata.labels['fs2.nebius.ai/network-policy-boundary'] == 'permanent' &&
              (
                request.userInfo.username == '${local.control_plane_network_policy_security_owner}' ||
                (
                  request.userInfo.username == 'system:serviceaccount:fs2-system:${local.control_plane_network_policy_service_account}' &&
                  (
                    (request.resource.group == 'networking.k8s.io' && request.resource.resource == 'networkpolicies' && 'fs2.nebius.ai/network-policy-role' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/network-policy-role'] == oldObject.metadata.labels['fs2.nebius.ai/network-policy-role']) ||
                    (request.resource.group == 'coordination.k8s.io' && request.resource.resource == 'leases' && request.namespace == 'fs2-system' && request.name == '${local.control_plane_network_policy_state_name}') ||
                    (request.resource.group == '' && request.resource.resource == 'configmaps' && request.namespace == 'fs2-system' && request.name == '${local.control_plane_network_policy_state_name}')
                  )
                )
              )
            )
          CEL
          message    = "permanent boundary updates require the exact transition identity and immutable ownership labels"
          reason     = "Forbidden"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_network_policy_v1.control_plane_public_envoy_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_controller_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_default_deny,
  ]
}

resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission_binding" {
  provider = kubernetes.network_policy_security_owner

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

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_manifest.control_plane_network_policy_boundary_admission,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller,
    kubernetes_config_map_v1.control_plane_network_policy_transition_receipt,
    kubernetes_manifest.control_plane_network_policy_transition_lease,
    kubernetes_config_map_v1.control_plane_network_policy_topology,
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner,
  ]
}
