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
  control_plane_network_policy_parameter_name       = "fs2-network-policy-boundary-parameters"
  control_plane_network_policy_gateway_namespace    = var.network_policy_boundary.gateway_namespace
  control_plane_network_policy_controller_namespace = var.network_policy_boundary.controller_namespace
  control_plane_network_policy_security_owner_kubeconfig_path = coalesce(
    var.network_policy_boundary.security_owner_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-security-owner-kubeconfig",
  )
  control_plane_network_policy_client_private_key_path = coalesce(
    var.network_policy_boundary.security_handoff_client_private_key_path,
    "${local.normalized_run_root}/network-policy-transition-client-ed25519",
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
  control_plane_network_policy_security_handoff = {
    schema                     = "fs2-serve.nebius.ai/network-policy-security-handoff/v2"
    socket_path                = var.network_policy_boundary.security_handoff_socket_path
    server_public_key          = var.network_policy_boundary.security_handoff_server_public_key
    server_public_key_sha256   = var.network_policy_boundary.security_handoff_server_public_key == null ? null : sha256(var.network_policy_boundary.security_handoff_server_public_key)
    client_public_key          = var.network_policy_boundary.security_handoff_client_public_key
    client_public_key_sha256   = var.network_policy_boundary.security_handoff_client_public_key == null ? null : sha256(var.network_policy_boundary.security_handoff_client_public_key)
    client_private_key_path    = local.control_plane_network_policy_client_private_key_path
    recovery_public_key        = var.network_policy_boundary.security_handoff_recovery_public_key
    recovery_public_key_sha256 = var.network_policy_boundary.security_handoff_recovery_public_key == null ? null : sha256(var.network_policy_boundary.security_handoff_recovery_public_key)
    peer_uid                   = var.network_policy_boundary.security_handoff_peer_uid
    peer_gid                   = var.network_policy_boundary.security_handoff_peer_gid
    cluster = {
      api_server_sha256 = sha256(coalesce(local.selected_api_server, ""))
      kube_system_uid   = var.kube_system_uid
    }
    allowed_actions = ["transition-mutation", "set-admission-recovery"]
    recovery_modes  = ["Audit", "Warn", "Deny"]
    delete_allowed  = false
  }
}

resource "terraform_data" "control_plane_network_policy_security_owner_preflight" {
  input = {
    ordinary_kubeconfig       = abspath(var.kubeconfig_path)
    security_owner_kubeconfig = abspath(local.control_plane_network_policy_security_owner_kubeconfig_path)
    client_private_key        = abspath(local.control_plane_network_policy_client_private_key_path)
    peer_uid                  = coalesce(var.network_policy_boundary.security_handoff_peer_uid, -1)
    peer_gid                  = coalesce(var.network_policy_boundary.security_handoff_peer_gid, -1)
    boundary_mode             = var.network_policy_boundary.mode
    kube_context              = var.kube_context
    kube_system_uid           = var.kube_system_uid
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      test -f "$FS2_SECURITY_KUBECONFIG"
      test "$(stat -c '%a' "$FS2_SECURITY_KUBECONFIG")" = "600"
      test "$(stat -c '%u' "$FS2_SECURITY_KUBECONFIG")" = "$(id -u)"
      test "$FS2_ORDINARY_KUBECONFIG" != "$FS2_SECURITY_KUBECONFIG"
      if test "$FS2_BOUNDARY_MODE" = public; then
        test "$(id -u)" != "$FS2_PEER_UID"
        test -f "$FS2_CLIENT_PRIVATE_KEY"
        case "$(stat -c '%a' "$FS2_CLIENT_PRIVATE_KEY")" in 400|600) ;; *) exit 1 ;; esac
        test "$(stat -c '%u' "$FS2_CLIENT_PRIVATE_KEY")" = "$FS2_PEER_UID"
        case " $(id -G) " in *" $FS2_PEER_GID "*) ;; *) exit 1 ;; esac
      fi
      ordinary_server="$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json | jq -er '.clusters | select(length == 1) | .[0].cluster.server')"
      security_server="$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json | jq -er '.clusters | select(length == 1) | .[0].cluster.server')"
      test "$ordinary_server" = "$security_server"
      ordinary_kube_system_uid="$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" get namespace kube-system -o 'jsonpath={.metadata.uid}')"
      security_kube_system_uid="$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" get namespace kube-system -o 'jsonpath={.metadata.uid}')"
      test "$ordinary_kube_system_uid" = "$FS2_KUBE_SYSTEM_UID"
      test "$security_kube_system_uid" = "$FS2_KUBE_SYSTEM_UID"
      test "$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | jq -r '.status.userInfo.username')" = "$FS2_SECURITY_OWNER_USERNAME"
      test "$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | jq -r '.status.userInfo.username')" != "$FS2_SECURITY_OWNER_USERNAME"
      ordinary_can() {
        kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      security_can() {
        kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      ordinary_named_can() {
        verb="$1"; resource="$2"; name="$3"; shift 3
        ordinary_can "$verb" "$resource" --resource-name="$name" "$@"
      }
      security_named_can() {
        verb="$1"; resource="$2"; name="$3"; shift 3
        security_can "$verb" "$resource" --resource-name="$name" "$@"
      }
      for resource in validatingadmissionpolicies.admissionregistration.k8s.io validatingadmissionpolicybindings.admissionregistration.k8s.io; do
        test "$(ordinary_named_can get "$resource" fs2-network-policy-boundary)" = "no"
        for verb in patch update; do
          test "$(ordinary_named_can "$verb" "$resource" fs2-network-policy-boundary)" = "no"
          test "$(security_named_can "$verb" "$resource" fs2-network-policy-boundary)" = "yes"
        done
        test "$(ordinary_named_can delete "$resource" fs2-network-policy-boundary)" = "no"
        test "$(security_named_can delete "$resource" fs2-network-policy-boundary)" = "no"
        test "$(ordinary_can deletecollection "$resource")" = "no"
        test "$(security_can deletecollection "$resource")" = "no"
      done
      while IFS='|' read -r namespace resource name; do
        test "$(ordinary_named_can get "$resource" "$name" --namespace "$namespace")" = "yes"
        test "$(security_named_can get "$resource" "$name" --namespace "$namespace")" = "yes"
        for verb in patch update; do
          test "$(ordinary_named_can "$verb" "$resource" "$name" --namespace "$namespace")" = "no"
          test "$(security_named_can "$verb" "$resource" "$name" --namespace "$namespace")" = "yes"
        done
        test "$(ordinary_named_can delete "$resource" "$name" --namespace "$namespace")" = "no"
        test "$(security_named_can delete "$resource" "$name" --namespace "$namespace")" = "no"
        test "$(ordinary_can deletecollection "$resource" --namespace "$namespace")" = "no"
        test "$(security_can deletecollection "$resource" --namespace "$namespace")" = "no"
      done <<EOF
$FS2_GATEWAY_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-public-envoy-transition-guard
$FS2_GATEWAY_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-envoy-default-deny
$FS2_CONTROLLER_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-envoy-controller-xds-transition-guard
fs2-system|configmaps|fs2-network-policy-transition
fs2-system|configmaps|fs2-network-policy-boundary-topology
fs2-system|configmaps|fs2-network-policy-boundary-parameters
fs2-system|leases.coordination.k8s.io|fs2-network-policy-transition
EOF
      for resource in users.authentication.k8s.io groups.authentication.k8s.io serviceaccounts uids.authentication.k8s.io userextras.authentication.k8s.io; do
        test "$(ordinary_can impersonate "$resource")" = "no"
      done
      test "$(ordinary_named_can impersonate users.authentication.k8s.io "$FS2_SECURITY_OWNER_USERNAME")" = "no"
      test "$(ordinary_named_can impersonate users.authentication.k8s.io fs2-network-policy-security-probe)" = "no"
      test "$(ordinary_named_can impersonate groups.authentication.k8s.io system:masters)" = "no"
      test "$(ordinary_named_can impersonate groups.authentication.k8s.io system:authenticated)" = "no"
      test "$(ordinary_named_can impersonate groups.authentication.k8s.io system:serviceaccounts)" = "no"
      test "$(ordinary_named_can impersonate groups.authentication.k8s.io system:serviceaccounts:fs2-system)" = "no"
      test "$(ordinary_named_can impersonate groups.authentication.k8s.io fs2-network-policy-security-probe)" = "no"
      test "$(ordinary_named_can impersonate serviceaccounts "system:serviceaccount:fs2-system:fs2-network-policy-transition")" = "no"
      test "$(ordinary_named_can impersonate serviceaccounts fs2-system:fs2-network-policy-security-probe)" = "no"
      test "$(ordinary_named_can impersonate uids.authentication.k8s.io "$FS2_PEER_UID")" = "no"
      test "$(ordinary_named_can impersonate uids.authentication.k8s.io 00000000-0000-4000-8000-000000000000)" = "no"
      test "$(ordinary_named_can impersonate userextras.authentication.k8s.io scopes)" = "no"
      test "$(ordinary_named_can impersonate userextras.authentication.k8s.io fs2.nebius.ai/security-probe)" = "no"
      test "$(ordinary_named_can create serviceaccounts fs2-network-policy-transition --subresource=token --namespace fs2-system)" = "no"
      while IFS='|' read -r namespace resource name; do
        namespace_args=()
        if test -n "$namespace"; then namespace_args=(--namespace "$namespace"); fi
        for verb in create patch update delete bind escalate; do
          test "$(ordinary_can "$verb" "$resource" "$${namespace_args[@]}")" = "no"
          test "$(ordinary_named_can "$verb" "$resource" "$name" "$${namespace_args[@]}")" = "no"
        done
        test "$(ordinary_can deletecollection "$resource" "$${namespace_args[@]}")" = "no"
        test "$(security_named_can bind "$resource" "$name" "$${namespace_args[@]}")" = "no"
        test "$(security_named_can escalate "$resource" "$name" "$${namespace_args[@]}")" = "no"
      done <<EOF
|clusterroles.rbac.authorization.k8s.io|$FS2_SECURITY_OWNER_USERNAME
|clusterrolebindings.rbac.authorization.k8s.io|$FS2_SECURITY_OWNER_USERNAME
fs2-system|roles.rbac.authorization.k8s.io|fs2-network-policy-transition
fs2-system|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition
$FS2_GATEWAY_NAMESPACE|roles.rbac.authorization.k8s.io|fs2-network-policy-transition-gateway
$FS2_GATEWAY_NAMESPACE|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition-gateway
$FS2_CONTROLLER_NAMESPACE|roles.rbac.authorization.k8s.io|fs2-network-policy-transition-controller
$FS2_CONTROLLER_NAMESPACE|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition-controller
EOF
      test "$(security_can create validatingadmissionpolicies.admissionregistration.k8s.io)" = "yes"
      test "$(security_can create validatingadmissionpolicybindings.admissionregistration.k8s.io)" = "yes"
    EOT
    environment = {
      FS2_ORDINARY_KUBECONFIG     = self.input.ordinary_kubeconfig
      FS2_SECURITY_KUBECONFIG     = self.input.security_owner_kubeconfig
      FS2_CLIENT_PRIVATE_KEY      = self.input.client_private_key
      FS2_PEER_UID                = tostring(self.input.peer_uid)
      FS2_PEER_GID                = tostring(self.input.peer_gid)
      FS2_BOUNDARY_MODE           = self.input.boundary_mode
      FS2_KUBE_CONTEXT            = self.input.kube_context
      FS2_KUBE_SYSTEM_UID         = self.input.kube_system_uid
      FS2_SECURITY_OWNER_USERNAME = local.control_plane_network_policy_security_owner
      FS2_GATEWAY_NAMESPACE       = local.control_plane_network_policy_gateway_namespace
      FS2_CONTROLLER_NAMESPACE    = local.control_plane_network_policy_controller_namespace
    }
  }

  lifecycle { prevent_destroy = true }
}

// Retain the pre-existing object at its stable Terraform address so this
// hardening change cannot plan a deletion. It is deliberately inert: no
// protected RoleBinding names it, token automount is disabled, and the
// ordinary rollout identity must be unable to mint a token for it.
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
      security_handoff        = local.control_plane_network_policy_security_handoff
      gateway_namespace       = local.control_plane_network_policy_gateway_namespace
      controller_namespace    = local.control_plane_network_policy_controller_namespace
      policy_names            = local.control_plane_network_policy_names
    })
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        var.network_policy_boundary.mode != "public" ||
        (
          var.network_policy_boundary.security_handoff_server_public_key != null &&
          var.network_policy_boundary.security_handoff_client_public_key != null &&
          var.network_policy_boundary.security_handoff_recovery_public_key != null &&
          var.network_policy_boundary.security_handoff_peer_uid != null &&
          var.network_policy_boundary.security_handoff_peer_gid != null
        )
      )
      error_message = "Public NetworkPolicy boundaries require pinned server, coordinator and recovery keys plus the exact Unix peer UID/GID."
    }
  }

  depends_on = [
    kubernetes_namespace_v1.platform,
    terraform_data.control_plane_network_policy_security_owner_preflight,
  ]
}

resource "kubernetes_config_map_v1" "control_plane_network_policy_boundary_parameters" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_parameter_name
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  data = {
    schema                 = "fs2-serve.nebius.ai/network-policy-boundary-parameters/v1"
    mode                   = "deny"
    delete_allowed         = "false"
    signer_key_id          = coalesce(local.control_plane_network_policy_security_handoff.server_public_key_sha256, "unconfigured")
    recovery_signer_key_id = coalesce(local.control_plane_network_policy_security_handoff.recovery_public_key_sha256, "unconfigured")
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

  # Creation is allows-first. All three objects are permanently retained and
  # every identity in this boundary is denied delete/deletecollection.
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
      local.control_plane_network_policy_parameter_name,
    ]
    verbs = ["get"]
  }
  rule {
    api_groups = [""]
    resources  = ["configmaps"]
    resource_names = [
      local.control_plane_network_policy_state_name,
      local.control_plane_network_policy_parameter_name,
    ]
    verbs = ["get", "patch", "update"]
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
    api_groups     = ["admissionregistration.k8s.io"]
    resources      = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
    resource_names = ["fs2-network-policy-boundary"]
    verbs          = ["get", "patch", "update"]
  }
  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    resource_names = distinct([
      "kube-system",
      "fs2-system",
      local.control_plane_network_policy_gateway_namespace,
      local.control_plane_network_policy_controller_namespace,
    ])
    verbs = ["get"]
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
    kind      = "User"
    name      = local.control_plane_network_policy_security_owner
    api_group = "rbac.authorization.k8s.io"
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
    kind      = "User"
    name      = local.control_plane_network_policy_security_owner
    api_group = "rbac.authorization.k8s.io"
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
    kind      = "User"
    name      = local.control_plane_network_policy_security_owner
    api_group = "rbac.authorization.k8s.io"
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
      paramKind = {
        apiVersion = "v1"
        kind       = "ConfigMap"
      }
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
            has(params) &&
            params.data['schema'] == 'fs2-serve.nebius.ai/network-policy-boundary-parameters/v1' &&
            params.data['delete_allowed'] == 'false' &&
            params.data['mode'] in ['deny', 'audit-warn']
          CEL
          message    = "permanent boundary parameters must remain signed-handoff governed and deletion-disabled"
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'DELETE'"
          message    = "permanent boundary objects are never deleted"
          reason     = "Forbidden"
        },
        {
          expression = <<-CEL
            request.operation != 'UPDATE' || (
              has(object.metadata.labels) &&
              object.metadata.labels['fs2.nebius.ai/network-policy-boundary'] == 'permanent' &&
              request.userInfo.username == '${local.control_plane_network_policy_security_owner}' &&
              (!('fs2.nebius.ai/network-policy-role' in oldObject.metadata.labels) ||
               ('fs2.nebius.ai/network-policy-role' in object.metadata.labels &&
                object.metadata.labels['fs2.nebius.ai/network-policy-role'] == oldObject.metadata.labels['fs2.nebius.ai/network-policy-role']))
            )
          CEL
          message    = "permanent boundary updates require the external security owner and immutable ownership labels"
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
      policyName = "fs2-network-policy-boundary"
      paramRef = {
        name                    = local.control_plane_network_policy_parameter_name
        namespace               = "fs2-system"
        parameterNotFoundAction = "Deny"
      }
      validationActions = ["Deny"]
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [manifest.spec.validationActions]
  }

  depends_on = [
    kubernetes_manifest.control_plane_network_policy_boundary_admission,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller,
    kubernetes_config_map_v1.control_plane_network_policy_transition_receipt,
    kubernetes_manifest.control_plane_network_policy_transition_lease,
    kubernetes_config_map_v1.control_plane_network_policy_topology,
    kubernetes_config_map_v1.control_plane_network_policy_boundary_parameters,
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner,
  ]
}
