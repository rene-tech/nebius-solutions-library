// This boundary deliberately lives in the foundation state, outside the
// workload/Helm lifecycle it protects. External security-owner IAM protects
// the admission objects; normal foundation destroy and targeted replacement
// are refused as a second independent guard.
locals {
  control_plane_network_policy_release_name               = "fs2-serve-control-plane"
  control_plane_network_policy_service_account            = "fs2-network-policy-transition"
  control_plane_network_policy_identity_suffix            = substr(sha256(coalesce(var.network_policy_boundary.identity_epoch, "unconfigured")), 0, 16)
  control_plane_network_policy_prior_suffix               = substr(sha256(coalesce(var.network_policy_boundary.prior_identity_epoch, "unconfigured-prior")), 0, 16)
  control_plane_network_policy_successor_suffix           = substr(sha256(coalesce(var.network_policy_boundary.successor_identity_epoch, "unconfigured-successor")), 0, 16)
  control_plane_network_policy_release_principal          = "fs2-np-release-${local.control_plane_network_policy_identity_suffix}"
  control_plane_network_policy_security_owner             = "fs2-np-security-owner-${local.control_plane_network_policy_identity_suffix}"
  control_plane_network_policy_security_bootstrap         = "fs2-np-security-bootstrap-${local.control_plane_network_policy_identity_suffix}"
  control_plane_network_policy_prior_security_owner       = "fs2-np-security-owner-${local.control_plane_network_policy_prior_suffix}"
  control_plane_network_policy_prior_bootstrap            = "fs2-np-security-bootstrap-${local.control_plane_network_policy_prior_suffix}"
  control_plane_network_policy_successor_owner            = "fs2-np-security-owner-${local.control_plane_network_policy_successor_suffix}"
  control_plane_network_policy_successor_bootstrap        = "fs2-np-security-bootstrap-${local.control_plane_network_policy_successor_suffix}"
  control_plane_network_policy_provider_trust_anchor_path = "/etc/fs2/security/network-policy-provider-trust-anchor-v2.json"
  control_plane_network_policy_provider_adapter_path = abspath(
    "${path.module}/../../components/control-plane/scripts/network_policy_subject_provider_adapter.py"
  )
  control_plane_network_policy_state_name           = "fs2-network-policy-transition"
  control_plane_network_policy_topology_name        = "fs2-network-policy-boundary-topology"
  control_plane_network_policy_parameter_name       = "fs2-network-policy-boundary-parameters"
  control_plane_network_policy_gateway_namespace    = var.network_policy_boundary.gateway_namespace
  control_plane_network_policy_controller_namespace = var.network_policy_boundary.controller_namespace
  control_plane_network_policy_security_owner_kubeconfig_path = coalesce(
    var.network_policy_boundary.security_owner_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-security-owner-kubeconfig",
  )
  control_plane_network_policy_security_bootstrap_kubeconfig_path = coalesce(
    var.network_policy_boundary.security_bootstrap_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-security-bootstrap-kubeconfig",
  )
  control_plane_network_policy_prior_security_owner_kubeconfig_path = coalesce(
    var.network_policy_boundary.prior_security_owner_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-prior-security-owner-kubeconfig",
  )
  control_plane_network_policy_prior_security_bootstrap_kubeconfig_path = coalesce(
    var.network_policy_boundary.prior_security_bootstrap_kubeconfig_path,
    "${local.normalized_run_root}/network-policy-prior-security-bootstrap-kubeconfig",
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
  control_plane_network_policy_bootstrap_names = {
    cluster    = "fs2-network-policy-security-bootstrap"
    state      = "${local.control_plane_network_policy_state_name}-bootstrap"
    gateway    = "${local.control_plane_network_policy_state_name}-gateway-bootstrap"
    controller = "${local.control_plane_network_policy_state_name}-controller-bootstrap"
  }
  control_plane_network_policy_boundary_labels = {
    "app.kubernetes.io/instance"            = local.control_plane_network_policy_release_name
    "app.kubernetes.io/managed-by"          = "terraform"
    "app.kubernetes.io/part-of"             = "fs2-serve"
    "fs2.nebius.ai/network-policy-boundary" = "permanent"
  }
  control_plane_network_policy_inventory_subjects = var.network_policy_boundary.security_subject_inventory == null ? [] : concat(
    var.network_policy_boundary.security_subject_inventory.signed.human_users,
    [
      for group in var.network_policy_boundary.security_subject_inventory.signed.human_groups : {
        username = "fs2-security-group-probe-${substr(sha256(group), 0, 12)}"
        groups   = [group]
      }
    ],
  )
  control_plane_network_policy_rollback_valid_until = (
    var.network_policy_boundary.release_identity == null ||
    var.network_policy_boundary.security_owner_identity == null
    ) ? null : (
    timecmp(
      var.network_policy_boundary.release_identity.expires_at,
      var.network_policy_boundary.security_owner_identity.expires_at,
    ) <= 0 ?
    var.network_policy_boundary.release_identity.expires_at :
    var.network_policy_boundary.security_owner_identity.expires_at
  )
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
    peer_gid_contract          = "effective-dedicated"
    socket_directory_contract  = "precreated-setgid-02710"
    identity_boundary = {
      schema                   = "fs2-serve.nebius.ai/network-policy-identity-boundary/v3"
      identity_epoch           = var.network_policy_boundary.identity_epoch
      prior_identity_epoch     = var.network_policy_boundary.prior_identity_epoch
      successor_identity_epoch = var.network_policy_boundary.successor_identity_epoch
      epoch_principals = {
        release             = local.control_plane_network_policy_release_principal
        security_owner      = local.control_plane_network_policy_security_owner
        security_bootstrap  = local.control_plane_network_policy_security_bootstrap
        prior_owner         = local.control_plane_network_policy_prior_security_owner
        prior_bootstrap     = local.control_plane_network_policy_prior_bootstrap
        successor_owner     = local.control_plane_network_policy_successor_owner
        successor_bootstrap = local.control_plane_network_policy_successor_bootstrap
      }
      release_user_info_sha256 = var.network_policy_boundary.release_identity == null ? null : sha256(jsonencode({
        username = var.network_policy_boundary.release_identity.username
        uid      = var.network_policy_boundary.release_identity.uid
        groups   = sort(var.network_policy_boundary.release_identity.groups)
        extra    = { for key, values in var.network_policy_boundary.release_identity.extra : key => sort(values) }
      }))
      security_user_info_sha256 = var.network_policy_boundary.security_owner_identity == null ? null : sha256(jsonencode({
        username = var.network_policy_boundary.security_owner_identity.username
        uid      = var.network_policy_boundary.security_owner_identity.uid
        groups   = sort(var.network_policy_boundary.security_owner_identity.groups)
        extra    = { for key, values in var.network_policy_boundary.security_owner_identity.extra : key => sort(values) }
      }))
      bootstrap_user_info_sha256 = var.network_policy_boundary.security_bootstrap_identity == null ? null : sha256(jsonencode({
        username = var.network_policy_boundary.security_bootstrap_identity.username
        uid      = var.network_policy_boundary.security_bootstrap_identity.uid
        groups   = sort(var.network_policy_boundary.security_bootstrap_identity.groups)
        extra    = { for key, values in var.network_policy_boundary.security_bootstrap_identity.extra : key => sort(values) }
      }))
      prior_security_user_info_sha256 = var.network_policy_boundary.prior_security_owner_identity == null ? null : sha256(jsonencode({
        username = var.network_policy_boundary.prior_security_owner_identity.username
        uid      = var.network_policy_boundary.prior_security_owner_identity.uid
        groups   = sort(var.network_policy_boundary.prior_security_owner_identity.groups)
        extra    = { for key, values in var.network_policy_boundary.prior_security_owner_identity.extra : key => sort(values) }
      }))
      prior_bootstrap_user_info_sha256 = var.network_policy_boundary.prior_security_bootstrap_identity == null ? null : sha256(jsonencode({
        username = var.network_policy_boundary.prior_security_bootstrap_identity.username
        uid      = var.network_policy_boundary.prior_security_bootstrap_identity.uid
        groups   = sort(var.network_policy_boundary.prior_security_bootstrap_identity.groups)
        extra    = { for key, values in var.network_policy_boundary.prior_security_bootstrap_identity.extra : key => sort(values) }
      }))
      credential_set_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.credential_set_sha256,
        null,
      )
      release_kubeconfig_sha256         = try(data.external.control_plane_network_policy_security_preflight_v2.result.release_kubeconfig_sha256, null)
      security_kubeconfig_sha256        = try(data.external.control_plane_network_policy_security_preflight_v2.result.security_kubeconfig_sha256, null)
      bootstrap_kubeconfig_sha256       = try(data.external.control_plane_network_policy_security_preflight_v2.result.bootstrap_kubeconfig_sha256, null)
      prior_security_kubeconfig_sha256  = try(data.external.control_plane_network_policy_security_preflight_v2.result.prior_security_kubeconfig_sha256, null)
      prior_bootstrap_kubeconfig_sha256 = try(data.external.control_plane_network_policy_security_preflight_v2.result.prior_bootstrap_kubeconfig_sha256, null)
      release_expires_at                = var.network_policy_boundary.release_identity == null ? null : var.network_policy_boundary.release_identity.expires_at
      security_expires_at               = var.network_policy_boundary.security_owner_identity == null ? null : var.network_policy_boundary.security_owner_identity.expires_at
      bootstrap_expires_at              = var.network_policy_boundary.security_bootstrap_identity == null ? null : var.network_policy_boundary.security_bootstrap_identity.expires_at
      rollback_valid_until              = local.control_plane_network_policy_rollback_valid_until
      minimum_rollback_seconds          = var.network_policy_boundary.minimum_rollback_seconds
      bootstrap_must_be_expired         = true
      permitted_shared_groups           = ["system:authenticated", "system:serviceaccounts"]
      security_subject_inventory_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.subject_inventory_sha256,
        null,
      )
      provider_subject_snapshot_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.provider_snapshot_sha256,
        null,
      )
      provider_trust_anchor_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.provider_trust_anchor_sha256,
        null,
      )
      provider_adapter_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.provider_adapter_sha256,
        null,
      )
      kubernetes_subject_inventory_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.kubernetes_subject_inventory_sha256,
        null,
      )
      auditor_bootstrap_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.auditor_bootstrap_sha256,
        null,
      )
      external_role_bundle_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.external_role_bundle_sha256,
        null,
      )
      plan_rotation_phase = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.rotation_phase,
        null,
      )
      rotation_binding_state_sha256 = try(
        data.external.control_plane_network_policy_security_preflight_v2.result.rotation_binding_state_sha256,
        null,
      )
      plan_preflight_verified = data.external.control_plane_network_policy_security_preflight_v2.result.verified == "true"
      plan_preflight_sha256   = data.external.control_plane_network_policy_security_preflight_v2.result.contract_sha256
      rotation_contract = {
        mechanism                                 = "preauthorized-successor-epoch"
        bootstrap_update_identity                 = local.control_plane_network_policy_security_bootstrap
        successor_security_owner_identity         = local.control_plane_network_policy_successor_owner
        successor_bootstrap_identity              = local.control_plane_network_policy_successor_bootstrap
        prior_security_owner_identity             = local.control_plane_network_policy_prior_security_owner
        prior_bootstrap_identity                  = local.control_plane_network_policy_prior_bootstrap
        new_paths_required                        = true
        allowed_plan_phases                       = ["preapply", "resume", "postapply"]
        mutation_binding_states                   = ["before", "target"]
        fresh_preapply_prior_owner_authorized     = true
        prior_bootstrap_protected_mutation_denied = true
        postapply_retirement_required             = true
      }
    }
    cluster = {
      api_server_sha256 = sha256(coalesce(local.selected_api_server, ""))
      kube_system_uid   = var.kube_system_uid
    }
    allowed_actions = ["transition-mutation", "set-admission-recovery"]
    recovery_modes  = ["Audit", "Warn", "Deny"]
    delete_allowed  = false
    auditor_bootstrap = {
      mechanism              = "external-preprovision-declarative-import"
      cluster_role           = "fs2-network-policy-security-auditor"
      cluster_role_binding   = "fs2-network-policy-security-auditor"
      bootstrap_cluster_role = local.control_plane_network_policy_bootstrap_names.cluster
      bootstrap_namespaced_roles = {
        state      = local.control_plane_network_policy_bootstrap_names.state
        gateway    = local.control_plane_network_policy_bootstrap_names.gateway
        controller = local.control_plane_network_policy_bootstrap_names.controller
      }
      immutable_role_bundle_sha256 = try(data.external.control_plane_network_policy_security_preflight_v2.result.external_role_bundle_sha256, null)
      preapply_subjects            = [local.control_plane_network_policy_prior_bootstrap, local.control_plane_network_policy_security_bootstrap]
      desired_subjects             = [local.control_plane_network_policy_security_bootstrap, local.control_plane_network_policy_successor_bootstrap]
      observed_sha256              = try(data.external.control_plane_network_policy_security_preflight_v2.result.auditor_bootstrap_sha256, null)
      bootstrap_create             = false
      bootstrap_exact_update       = true
      delete_allowed               = false
    }
  }
}

data "external" "control_plane_network_policy_security_preflight_v2" {
  program = [
    "python3",
    "${path.module}/scripts/verify-network-policy-security-preflight.py",
  ]
  query = {
    mode                                  = var.network_policy_boundary.mode
    context                               = var.kube_context
    kube_system_uid                       = var.kube_system_uid
    release_kubeconfig                    = abspath(var.kubeconfig_path)
    security_kubeconfig                   = abspath(local.control_plane_network_policy_security_owner_kubeconfig_path)
    bootstrap_kubeconfig                  = abspath(local.control_plane_network_policy_security_bootstrap_kubeconfig_path)
    prior_security_kubeconfig             = abspath(local.control_plane_network_policy_prior_security_owner_kubeconfig_path)
    prior_bootstrap_kubeconfig            = abspath(local.control_plane_network_policy_prior_security_bootstrap_kubeconfig_path)
    release_identity                      = jsonencode(var.network_policy_boundary.release_identity)
    security_identity                     = jsonencode(var.network_policy_boundary.security_owner_identity)
    bootstrap_identity                    = jsonencode(var.network_policy_boundary.security_bootstrap_identity)
    prior_security_identity               = jsonencode(var.network_policy_boundary.prior_security_owner_identity)
    prior_bootstrap_identity              = jsonencode(var.network_policy_boundary.prior_security_bootstrap_identity)
    subject_inventory                     = jsonencode(var.network_policy_boundary.security_subject_inventory)
    provider_snapshot_path                = coalesce(var.network_policy_boundary.security_subject_provider_snapshot_path, "")
    provider_trust_anchor_path            = local.control_plane_network_policy_provider_trust_anchor_path
    provider_adapter_path                 = local.control_plane_network_policy_provider_adapter_path
    recovery_public_key                   = coalesce(var.network_policy_boundary.security_handoff_recovery_public_key, "")
    minimum_rollback_seconds              = tostring(var.network_policy_boundary.minimum_rollback_seconds)
    gateway_namespace                     = local.control_plane_network_policy_gateway_namespace
    controller_namespace                  = local.control_plane_network_policy_controller_namespace
    security_owner_username               = local.control_plane_network_policy_security_owner
    security_bootstrap_username           = local.control_plane_network_policy_security_bootstrap
    prior_security_owner_username         = local.control_plane_network_policy_prior_security_owner
    prior_security_bootstrap_username     = local.control_plane_network_policy_prior_bootstrap
    successor_security_owner_username     = local.control_plane_network_policy_successor_owner
    successor_security_bootstrap_username = local.control_plane_network_policy_successor_bootstrap
    peer_uid                              = tostring(coalesce(var.network_policy_boundary.security_handoff_peer_uid, -1))
    peer_gid                              = tostring(coalesce(var.network_policy_boundary.security_handoff_peer_gid, -1))
    socket_path                           = var.network_policy_boundary.security_handoff_socket_path
    identity_epoch                        = coalesce(var.network_policy_boundary.identity_epoch, "unconfigured")
    prior_identity_epoch                  = coalesce(var.network_policy_boundary.prior_identity_epoch, "unconfigured-prior")
    successor_identity_epoch              = coalesce(var.network_policy_boundary.successor_identity_epoch, "unconfigured-successor")
  }

  lifecycle {
    postcondition {
      condition = (
        self.result.verified == "true" &&
        can(regex("^[0-9a-f]{64}$", self.result.contract_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.subject_inventory_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.provider_snapshot_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.provider_trust_anchor_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.provider_adapter_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.kubernetes_subject_inventory_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.auditor_bootstrap_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.external_role_bundle_sha256)) &&
        contains(["preapply", "resume", "postapply"], self.result.rotation_phase) &&
        can(regex("^[0-9a-f]{64}$", self.result.rotation_binding_state_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.credential_set_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.release_kubeconfig_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.security_kubeconfig_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.bootstrap_kubeconfig_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.prior_security_kubeconfig_sha256)) &&
        can(regex("^[0-9a-f]{64}$", self.result.prior_bootstrap_kubeconfig_sha256))
      )
      error_message = "The plan-time external security-boundary proof did not complete exactly."
    }
  }
}

resource "terraform_data" "control_plane_network_policy_security_owner_preflight" {
  input = {
    ordinary_kubeconfig           = abspath(var.kubeconfig_path)
    security_owner_kubeconfig     = abspath(local.control_plane_network_policy_security_owner_kubeconfig_path)
    security_bootstrap_kubeconfig = abspath(local.control_plane_network_policy_security_bootstrap_kubeconfig_path)
    prior_security_kubeconfig     = abspath(local.control_plane_network_policy_prior_security_owner_kubeconfig_path)
    prior_bootstrap_kubeconfig    = abspath(local.control_plane_network_policy_prior_security_bootstrap_kubeconfig_path)
    release_identity              = jsonencode(var.network_policy_boundary.release_identity)
    security_identity             = jsonencode(var.network_policy_boundary.security_owner_identity)
    bootstrap_identity            = jsonencode(var.network_policy_boundary.security_bootstrap_identity)
    prior_security_identity       = jsonencode(var.network_policy_boundary.prior_security_owner_identity)
    prior_bootstrap_identity      = jsonencode(var.network_policy_boundary.prior_security_bootstrap_identity)
    inventory_subjects            = jsonencode(local.control_plane_network_policy_inventory_subjects)
    plan_preflight_sha256         = data.external.control_plane_network_policy_security_preflight_v2.result.contract_sha256
    client_private_key            = abspath(local.control_plane_network_policy_client_private_key_path)
    peer_uid                      = coalesce(var.network_policy_boundary.security_handoff_peer_uid, -1)
    peer_gid                      = coalesce(var.network_policy_boundary.security_handoff_peer_gid, -1)
    socket_path                   = var.network_policy_boundary.security_handoff_socket_path
    identity_epoch                = coalesce(var.network_policy_boundary.identity_epoch, "unconfigured")
    prior_identity_epoch          = coalesce(var.network_policy_boundary.prior_identity_epoch, "unconfigured-prior")
    successor_identity_epoch      = coalesce(var.network_policy_boundary.successor_identity_epoch, "unconfigured-successor")
    minimum_rollback_seconds      = var.network_policy_boundary.minimum_rollback_seconds
    boundary_mode                 = var.network_policy_boundary.mode
    kube_context                  = var.kube_context
    kube_system_uid               = var.kube_system_uid
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      test -f "$FS2_SECURITY_KUBECONFIG"
      test -f "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG"
      test "$(stat -c '%a' "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG")" = "600"
      test "$(stat -c '%u' "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG")" = "$(id -u)"
      test "$(stat -c '%a' "$FS2_SECURITY_KUBECONFIG")" = "600"
      test "$(stat -c '%u' "$FS2_SECURITY_KUBECONFIG")" = "$(id -u)"
      test "$FS2_ORDINARY_KUBECONFIG" != "$FS2_SECURITY_KUBECONFIG"
      test "$FS2_ORDINARY_KUBECONFIG" != "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG"
      test "$FS2_SECURITY_KUBECONFIG" != "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG"
      if test "$FS2_BOUNDARY_MODE" = public; then
        test "$(id -u)" != "$FS2_PEER_UID"
        test "$(basename "$(dirname "$FS2_SOCKET_PATH")")" = "$FS2_IDENTITY_EPOCH"
        test ! -e "$FS2_SOCKET_PATH"
        socket_parent="$(dirname "$FS2_SOCKET_PATH")"
        test -d "$socket_parent"
        test "$(stat -c '%u' "$socket_parent")" = "$(id -u)"
        test "$(stat -c '%g' "$socket_parent")" = "$FS2_PEER_GID"
        test "$(stat -c '%a' "$socket_parent")" = "2710"
        test -f "$FS2_CLIENT_PRIVATE_KEY"
        case "$(stat -c '%a' "$FS2_CLIENT_PRIVATE_KEY")" in 400|600) ;; *) exit 1 ;; esac
        test "$(stat -c '%u' "$FS2_CLIENT_PRIVATE_KEY")" = "$FS2_PEER_UID"
        test "$(id -g)" = "$FS2_PEER_GID"
      fi
      ordinary_server="$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json | jq -er '.clusters | select(length == 1) | .[0].cluster.server')"
      security_server="$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json | jq -er '.clusters | select(length == 1) | .[0].cluster.server')"
      bootstrap_server="$(kubectl --kubeconfig "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json | jq -er '.clusters | select(length == 1) | .[0].cluster.server')"
      test "$ordinary_server" = "$security_server"
      test "$ordinary_server" = "$bootstrap_server"
      ordinary_kube_system_uid="$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" get namespace kube-system -o 'jsonpath={.metadata.uid}')"
      security_kube_system_uid="$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" get namespace kube-system -o 'jsonpath={.metadata.uid}')"
      bootstrap_kube_system_uid="$(kubectl --kubeconfig "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" get namespace kube-system -o 'jsonpath={.metadata.uid}')"
      test "$ordinary_kube_system_uid" = "$FS2_KUBE_SYSTEM_UID"
      test "$security_kube_system_uid" = "$FS2_KUBE_SYSTEM_UID"
      test "$bootstrap_kube_system_uid" = "$FS2_KUBE_SYSTEM_UID"
      if test "$FS2_BOUNDARY_MODE" = public; then
      normalize_identity() {
        jq -ceS '.status.userInfo | {username,uid,groups:((.groups // []) | sort),extra:((.extra // {}) | with_entries(.value |= sort))}'
      }
      ordinary_identity="$(kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | normalize_identity)"
      security_identity="$(kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | normalize_identity)"
      bootstrap_identity="$(kubectl --kubeconfig "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth whoami -o json | normalize_identity)"
      expected_identity() { jq -ceS '{username,uid,groups:(.groups|sort),extra:(.extra|with_entries(.value |= sort))}' <<<"$1"; }
      test "$ordinary_identity" = "$(expected_identity "$FS2_RELEASE_IDENTITY")"
      test "$security_identity" = "$(expected_identity "$FS2_SECURITY_IDENTITY")"
      test "$bootstrap_identity" = "$(expected_identity "$FS2_BOOTSTRAP_IDENTITY")"
      test "$(jq -r .username <<<"$ordinary_identity")" != "$(jq -r .username <<<"$security_identity")"
      test "$(jq -r .username <<<"$ordinary_identity")" != "$(jq -r .username <<<"$bootstrap_identity")"
      test "$(jq -r .username <<<"$security_identity")" != "$(jq -r .username <<<"$bootstrap_identity")"
      test "$(jq -r .uid <<<"$ordinary_identity")" != "$(jq -r .uid <<<"$security_identity")"
      test "$(jq -r .uid <<<"$ordinary_identity")" != "$(jq -r .uid <<<"$bootstrap_identity")"
      test "$(jq -r .uid <<<"$security_identity")" != "$(jq -r .uid <<<"$bootstrap_identity")"
      shared_groups_are_bounded() {
        jq -en --argjson left "$1" --argjson right "$2" '
          (($left.groups - ($left.groups - $right.groups)) - ["system:authenticated", "system:serviceaccounts"]) == []
        '
      }
      shared_groups_are_bounded "$ordinary_identity" "$security_identity"
      shared_groups_are_bounded "$ordinary_identity" "$bootstrap_identity"
      shared_groups_are_bounded "$security_identity" "$bootstrap_identity"
      credential_expiry_epoch() {
        kubeconfig="$1"
        config="$(kubectl --kubeconfig "$kubeconfig" --context "$FS2_KUBE_CONTEXT" config view --minify --raw -o json)"
        token="$(jq -r '.users | select(length == 1) | .[0].user.token // empty' <<<"$config")"
        if test -n "$token"; then
          payload="$${token#*.}"
          payload="$${payload%%.*}"
          remainder=$(( $${#payload} % 4 ))
          case "$remainder" in
            0) ;;
            2) payload="$${payload}==" ;;
            3) payload="$${payload}=" ;;
            *) return 1 ;;
          esac
          printf '%s' "$payload" | tr '_-' '/+' | base64 -d 2>/dev/null | jq -er '.exp | numbers'
          return
        fi
        certificate="$(jq -r '.users | select(length == 1) | .[0].user["client-certificate-data"] // empty' <<<"$config")"
        test -n "$certificate"
        not_after="$(printf '%s' "$certificate" | base64 -d | openssl x509 -noout -enddate | sed 's/^notAfter=//')"
        date -u -d "$not_after" +%s
      }
      now_epoch="$(date -u +%s)"
      release_expiry="$(date -u -d "$(jq -r .expires_at <<<"$FS2_RELEASE_IDENTITY")" +%s)"
      security_expiry="$(date -u -d "$(jq -r .expires_at <<<"$FS2_SECURITY_IDENTITY")" +%s)"
      bootstrap_expiry="$(date -u -d "$(jq -r .expires_at <<<"$FS2_BOOTSTRAP_IDENTITY")" +%s)"
      test "$release_expiry" -gt "$now_epoch" && test "$release_expiry" -le "$((now_epoch + 28800))"
      test "$security_expiry" -gt "$now_epoch" && test "$security_expiry" -le "$((now_epoch + 28800))"
      test "$bootstrap_expiry" -gt "$now_epoch" && test "$bootstrap_expiry" -le "$((now_epoch + 900))"
      test "$release_expiry" -ge "$((bootstrap_expiry + FS2_MINIMUM_ROLLBACK_SECONDS))"
      test "$security_expiry" -ge "$((bootstrap_expiry + FS2_MINIMUM_ROLLBACK_SECONDS))"
      test "$(credential_expiry_epoch "$FS2_ORDINARY_KUBECONFIG")" = "$release_expiry"
      test "$(credential_expiry_epoch "$FS2_SECURITY_KUBECONFIG")" = "$security_expiry"
      test "$(credential_expiry_epoch "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG")" = "$bootstrap_expiry"
      fi
      ordinary_can() {
        kubectl --kubeconfig "$FS2_ORDINARY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      security_can() {
        kubectl --kubeconfig "$FS2_SECURITY_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      bootstrap_can() {
        kubectl --kubeconfig "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" auth can-i "$@"
      }
      ordinary_named_can() {
        verb="$1"; resource="$2"; name="$3"; shift 3
        ordinary_can "$verb" "$resource" --resource-name="$name" "$@"
      }
      security_named_can() {
        verb="$1"; resource="$2"; name="$3"; shift 3
        security_can "$verb" "$resource" --resource-name="$name" "$@"
      }
      bootstrap_named_can() {
        verb="$1"; resource="$2"; name="$3"; shift 3
        bootstrap_can "$verb" "$resource" --resource-name="$name" "$@"
      }
      subject_allowed() {
        user="$1"; groups="$2"; verb="$3"; api_group="$4"; resource="$5"; namespace="$6"; name="$7"; subresource="$8"
        jq -nc \
          --arg user "$user" --argjson groups "$groups" --arg verb "$verb" --arg group "$api_group" \
          --arg resource "$resource" --arg namespace "$namespace" --arg name "$name" --arg subresource "$subresource" '
          {
            apiVersion:"authorization.k8s.io/v1", kind:"SubjectAccessReview",
            spec:{user:$user,groups:$groups,resourceAttributes:
              ({verb:$verb,group:$group,resource:$resource}
               + (if $namespace == "" then {} else {namespace:$namespace} end)
               + (if $name == "" then {} else {name:$name} end)
               + (if $subresource == "" then {} else {subresource:$subresource} end))}
          }' |
          kubectl --kubeconfig "$FS2_SECURITY_BOOTSTRAP_KUBECONFIG" --context "$FS2_KUBE_CONTEXT" \
            create --raw /apis/authorization.k8s.io/v1/subjectaccessreviews -f - |
          jq -r 'if (.status.allowed | type) == "boolean" then .status.allowed else error("missing SAR result") end'
      }
      subject_denied() { test "$(subject_allowed "$@")" = "false"; }
      for resource in validatingadmissionpolicies.admissionregistration.k8s.io validatingadmissionpolicybindings.admissionregistration.k8s.io; do
        test "$(ordinary_named_can get "$resource" fs2-network-policy-boundary)" = "no"
        for verb in patch update; do
          test "$(ordinary_named_can "$verb" "$resource" fs2-network-policy-boundary)" = "no"
          test "$(security_named_can "$verb" "$resource" fs2-network-policy-boundary)" = "yes"
          test "$(bootstrap_named_can "$verb" "$resource" fs2-network-policy-boundary)" = "yes"
          test "$(security_can "$verb" "$resource")" = "no"
          test "$(bootstrap_can "$verb" "$resource")" = "no"
        done
        test "$(ordinary_named_can delete "$resource" fs2-network-policy-boundary)" = "no"
        test "$(security_named_can delete "$resource" fs2-network-policy-boundary)" = "no"
        test "$(bootstrap_named_can delete "$resource" fs2-network-policy-boundary)" = "no"
        test "$(security_can delete "$resource")" = "no"
        test "$(ordinary_can deletecollection "$resource")" = "no"
        test "$(security_can deletecollection "$resource")" = "no"
        test "$(bootstrap_can deletecollection "$resource")" = "no"
      done
      test "$(bootstrap_can create subjectaccessreviews.authorization.k8s.io)" = "yes"
      for resource in validatingadmissionpolicies.admissionregistration.k8s.io validatingadmissionpolicybindings.admissionregistration.k8s.io; do
        test "$(security_can create "$resource")" = "no"
        test "$(bootstrap_can create "$resource")" = "no"
      done
      while IFS='|' read -r namespace resource name; do
        test "$(ordinary_named_can get "$resource" "$name" --namespace "$namespace")" = "yes"
        test "$(security_named_can get "$resource" "$name" --namespace "$namespace")" = "yes"
        security_update_expected=yes
        if test "$name" = fs2-network-policy-boundary-topology; then security_update_expected=no; fi
        for verb in patch update; do
          test "$(ordinary_named_can "$verb" "$resource" "$name" --namespace "$namespace")" = "no"
          test "$(security_named_can "$verb" "$resource" "$name" --namespace "$namespace")" = "$security_update_expected"
          test "$(bootstrap_named_can "$verb" "$resource" "$name" --namespace "$namespace")" = "yes"
          test "$(security_can "$verb" "$resource" --namespace "$namespace")" = "no"
          test "$(bootstrap_can "$verb" "$resource" --namespace "$namespace")" = "no"
        done
        test "$(ordinary_named_can delete "$resource" "$name" --namespace "$namespace")" = "no"
        test "$(security_named_can delete "$resource" "$name" --namespace "$namespace")" = "no"
        test "$(bootstrap_named_can delete "$resource" "$name" --namespace "$namespace")" = "no"
        test "$(security_can delete "$resource" --namespace "$namespace")" = "no"
        test "$(ordinary_can deletecollection "$resource" --namespace "$namespace")" = "no"
        test "$(security_can deletecollection "$resource" --namespace "$namespace")" = "no"
        test "$(bootstrap_can deletecollection "$resource" --namespace "$namespace")" = "no"
      done <<EOF
$FS2_GATEWAY_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-public-envoy-transition-guard
$FS2_GATEWAY_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-envoy-default-deny
$FS2_CONTROLLER_NAMESPACE|networkpolicies.networking.k8s.io|fs2-serve-control-plane-envoy-controller-xds-transition-guard
fs2-system|configmaps|fs2-network-policy-transition
fs2-system|configmaps|fs2-network-policy-boundary-topology
fs2-system|configmaps|fs2-network-policy-boundary-parameters
fs2-system|leases.coordination.k8s.io|fs2-network-policy-transition
EOF
      # The every-plan Python preflight supplies the authoritative paginated
      # namespace/ServiceAccount/CSR proof. These apply-time checks repeat its
      # API-group-sensitive global negatives without replacing that receipt.
      for resource in users groups serviceaccounts uids.authentication.k8s.io userextras.authentication.k8s.io; do
        test "$(ordinary_can impersonate "$resource")" = "no"
        test "$(security_can impersonate "$resource")" = "no"
        test "$(bootstrap_can impersonate "$resource")" = "no"
      done
      while IFS='|' read -r resource name; do
        test "$(ordinary_named_can impersonate "$resource" "$name")" = "no"
        test "$(security_named_can impersonate "$resource" "$name")" = "no"
        test "$(bootstrap_named_can impersonate "$resource" "$name")" = "no"
      done <<EOF
users|$FS2_SECURITY_OWNER_USERNAME
users|$FS2_SECURITY_BOOTSTRAP_USERNAME
users|$FS2_SUCCESSOR_SECURITY_OWNER_USERNAME
users|$FS2_SUCCESSOR_SECURITY_BOOTSTRAP_USERNAME
users|fs2-network-policy-security-probe
groups|system:masters
groups|system:authenticated
groups|system:serviceaccounts
groups|system:serviceaccounts:fs2-system
groups|fs2-network-policy-security-probe
serviceaccounts|system:serviceaccount:fs2-system:fs2-network-policy-transition
serviceaccounts|fs2-system:fs2-network-policy-security-probe
uids.authentication.k8s.io|$FS2_PEER_UID
uids.authentication.k8s.io|00000000-0000-4000-8000-000000000000
userextras.authentication.k8s.io|scopes
userextras.authentication.k8s.io|fs2.nebius.ai/security-probe
EOF
      test "$(ordinary_named_can create serviceaccounts fs2-network-policy-transition --subresource=token --namespace fs2-system)" = "no"
      test "$(security_named_can create serviceaccounts fs2-network-policy-transition --subresource=token --namespace fs2-system)" = "no"
      test "$(bootstrap_named_can create serviceaccounts fs2-network-policy-transition --subresource=token --namespace fs2-system)" = "no"
      test "$(ordinary_can create serviceaccounts --subresource=token --namespace fs2-system)" = "no"
      test "$(security_can create serviceaccounts --subresource=token --namespace fs2-system)" = "no"
      test "$(bootstrap_can create serviceaccounts --subresource=token --namespace fs2-system)" = "no"
      test "$(ordinary_can create serviceaccounts --subresource=token --all-namespaces)" = "no"
      test "$(security_can create serviceaccounts --subresource=token --all-namespaces)" = "no"
      test "$(bootstrap_can create serviceaccounts --subresource=token --all-namespaces)" = "no"
      for namespace in fs2-system "$FS2_GATEWAY_NAMESPACE" "$FS2_CONTROLLER_NAMESPACE"; do
        test "$(ordinary_can create serviceaccounts --subresource=token --namespace "$namespace")" = "no"
        test "$(security_can create serviceaccounts --subresource=token --namespace "$namespace")" = "no"
        test "$(bootstrap_can create serviceaccounts --subresource=token --namespace "$namespace")" = "no"
      done
      for verb in update delete; do
        test "$(ordinary_can "$verb" namespaces)" = "no"
        test "$(security_can "$verb" namespaces)" = "no"
        test "$(bootstrap_can "$verb" namespaces)" = "no"
        test "$(ordinary_named_can "$verb" namespaces fs2-system)" = "no"
        test "$(ordinary_named_can "$verb" namespaces "$FS2_GATEWAY_NAMESPACE")" = "no"
        test "$(ordinary_named_can "$verb" namespaces "$FS2_CONTROLLER_NAMESPACE")" = "no"
        test "$(security_named_can "$verb" namespaces fs2-system)" = "no"
        test "$(security_named_can "$verb" namespaces "$FS2_GATEWAY_NAMESPACE")" = "no"
        test "$(security_named_can "$verb" namespaces "$FS2_CONTROLLER_NAMESPACE")" = "no"
        test "$(bootstrap_named_can "$verb" namespaces fs2-system)" = "no"
        test "$(bootstrap_named_can "$verb" namespaces "$FS2_GATEWAY_NAMESPACE")" = "no"
        test "$(bootstrap_named_can "$verb" namespaces "$FS2_CONTROLLER_NAMESPACE")" = "no"
      done
      test "$(ordinary_can update namespaces/finalize)" = "no"
      test "$(security_can update namespaces/finalize)" = "no"
      test "$(bootstrap_can update namespaces/finalize)" = "no"
      test "$(ordinary_named_can update namespaces/finalize fs2-system)" = "no"
      test "$(ordinary_named_can update namespaces/finalize "$FS2_GATEWAY_NAMESPACE")" = "no"
      test "$(ordinary_named_can update namespaces/finalize "$FS2_CONTROLLER_NAMESPACE")" = "no"
      test "$(security_named_can update namespaces/finalize fs2-system)" = "no"
      test "$(security_named_can update namespaces/finalize "$FS2_GATEWAY_NAMESPACE")" = "no"
      test "$(security_named_can update namespaces/finalize "$FS2_CONTROLLER_NAMESPACE")" = "no"
      test "$(bootstrap_named_can update namespaces/finalize fs2-system)" = "no"
      test "$(bootstrap_named_can update namespaces/finalize "$FS2_GATEWAY_NAMESPACE")" = "no"
      test "$(bootstrap_named_can update namespaces/finalize "$FS2_CONTROLLER_NAMESPACE")" = "no"
      jq -c '.[]' <<<"$FS2_SECURITY_INVENTORY_SUBJECTS" | while IFS= read -r subject; do
        human_user="$(jq -r .username <<<"$subject")"
        human_groups="$(jq -c '.groups | sort' <<<"$subject")"
        while IFS='|' read -r api_group resource namespace name; do
          for verb in patch update delete; do
            subject_denied "$human_user" "$human_groups" "$verb" "$api_group" "$resource" "$namespace" "$name" ""
          done
        done <<EOF
admissionregistration.k8s.io|validatingadmissionpolicies||fs2-network-policy-boundary
admissionregistration.k8s.io|validatingadmissionpolicybindings||fs2-network-policy-boundary
networking.k8s.io|networkpolicies|$FS2_GATEWAY_NAMESPACE|fs2-serve-control-plane-public-envoy-transition-guard
networking.k8s.io|networkpolicies|$FS2_GATEWAY_NAMESPACE|fs2-serve-control-plane-envoy-default-deny
networking.k8s.io|networkpolicies|$FS2_CONTROLLER_NAMESPACE|fs2-serve-control-plane-envoy-controller-xds-transition-guard
|configmaps|fs2-system|fs2-network-policy-transition
|configmaps|fs2-system|fs2-network-policy-boundary-topology
|configmaps|fs2-system|fs2-network-policy-boundary-parameters
coordination.k8s.io|leases|fs2-system|fs2-network-policy-transition
EOF
        for namespace in fs2-system "$FS2_GATEWAY_NAMESPACE" "$FS2_CONTROLLER_NAMESPACE"; do
          subject_denied "$human_user" "$human_groups" update "" namespaces "" "$namespace" ""
          subject_denied "$human_user" "$human_groups" delete "" namespaces "" "$namespace" ""
          subject_denied "$human_user" "$human_groups" update "" namespaces "" "$namespace" finalize
        done
        subject_denied "$human_user" "$human_groups" create "" serviceaccounts fs2-system fs2-network-policy-transition token
        while IFS='|' read -r api_group resource; do
          subject_denied "$human_user" "$human_groups" impersonate "$api_group" "$resource" "" "" ""
        done <<EOF
        |users
        |groups
        |serviceaccounts
authentication.k8s.io|uids
authentication.k8s.io|userextras
EOF
        for verb in bind escalate; do
          subject_denied "$human_user" "$human_groups" "$verb" rbac.authorization.k8s.io clusterroles "" "" ""
          subject_denied "$human_user" "$human_groups" "$verb" rbac.authorization.k8s.io roles fs2-system "" ""
          subject_denied "$human_user" "$human_groups" "$verb" rbac.authorization.k8s.io clusterroles "" fs2-network-policy-security-owner ""
          subject_denied "$human_user" "$human_groups" "$verb" rbac.authorization.k8s.io roles fs2-system fs2-network-policy-transition ""
        done
      done
      while IFS='|' read -r namespace resource name; do
        namespace_args=()
        if test -n "$namespace"; then namespace_args=(--namespace "$namespace"); fi
        for verb in create patch update delete bind escalate; do
          test "$(ordinary_can "$verb" "$resource" "$${namespace_args[@]}")" = "no"
          test "$(ordinary_named_can "$verb" "$resource" "$name" "$${namespace_args[@]}")" = "no"
          test "$(security_can "$verb" "$resource" "$${namespace_args[@]}")" = "no"
          test "$(security_named_can "$verb" "$resource" "$name" "$${namespace_args[@]}")" = "no"
        done
        test "$(ordinary_can deletecollection "$resource" "$${namespace_args[@]}")" = "no"
        test "$(security_named_can bind "$resource" "$name" "$${namespace_args[@]}")" = "no"
        test "$(security_named_can escalate "$resource" "$name" "$${namespace_args[@]}")" = "no"
        test "$(bootstrap_named_can bind "$resource" "$name" "$${namespace_args[@]}")" = "no"
        test "$(bootstrap_named_can escalate "$resource" "$name" "$${namespace_args[@]}")" = "no"
      done <<EOF
|clusterroles.rbac.authorization.k8s.io|fs2-network-policy-security-owner
|clusterrolebindings.rbac.authorization.k8s.io|fs2-network-policy-security-owner
fs2-system|roles.rbac.authorization.k8s.io|fs2-network-policy-transition
fs2-system|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition
$FS2_GATEWAY_NAMESPACE|roles.rbac.authorization.k8s.io|fs2-network-policy-transition-gateway
$FS2_GATEWAY_NAMESPACE|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition-gateway
$FS2_CONTROLLER_NAMESPACE|roles.rbac.authorization.k8s.io|fs2-network-policy-transition-controller
$FS2_CONTROLLER_NAMESPACE|rolebindings.rbac.authorization.k8s.io|fs2-network-policy-transition-controller
EOF
    EOT
    environment = {
      FS2_ORDINARY_KUBECONFIG                   = self.input.ordinary_kubeconfig
      FS2_SECURITY_KUBECONFIG                   = self.input.security_owner_kubeconfig
      FS2_SECURITY_BOOTSTRAP_KUBECONFIG         = self.input.security_bootstrap_kubeconfig
      FS2_RELEASE_IDENTITY                      = self.input.release_identity
      FS2_SECURITY_IDENTITY                     = self.input.security_identity
      FS2_BOOTSTRAP_IDENTITY                    = self.input.bootstrap_identity
      FS2_SECURITY_INVENTORY_SUBJECTS           = self.input.inventory_subjects
      FS2_CLIENT_PRIVATE_KEY                    = self.input.client_private_key
      FS2_PEER_UID                              = tostring(self.input.peer_uid)
      FS2_PEER_GID                              = tostring(self.input.peer_gid)
      FS2_SOCKET_PATH                           = self.input.socket_path
      FS2_IDENTITY_EPOCH                        = self.input.identity_epoch
      FS2_MINIMUM_ROLLBACK_SECONDS              = tostring(self.input.minimum_rollback_seconds)
      FS2_BOUNDARY_MODE                         = self.input.boundary_mode
      FS2_KUBE_CONTEXT                          = self.input.kube_context
      FS2_KUBE_SYSTEM_UID                       = self.input.kube_system_uid
      FS2_SECURITY_OWNER_USERNAME               = local.control_plane_network_policy_security_owner
      FS2_SECURITY_BOOTSTRAP_USERNAME           = local.control_plane_network_policy_security_bootstrap
      FS2_SUCCESSOR_SECURITY_OWNER_USERNAME     = local.control_plane_network_policy_successor_owner
      FS2_SUCCESSOR_SECURITY_BOOTSTRAP_USERNAME = local.control_plane_network_policy_successor_bootstrap
      FS2_GATEWAY_NAMESPACE                     = local.control_plane_network_policy_gateway_namespace
      FS2_CONTROLLER_NAMESPACE                  = local.control_plane_network_policy_controller_namespace
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
    annotations = {
      "fs2.nebius.ai/network-policy-identity-epoch"        = coalesce(var.network_policy_boundary.identity_epoch, "unconfigured")
      "fs2.nebius.ai/network-policy-successor-epoch"       = coalesce(var.network_policy_boundary.successor_identity_epoch, "unconfigured-successor")
      "fs2.nebius.ai/network-policy-credential-set-sha256" = data.external.control_plane_network_policy_security_preflight_v2.result.credential_set_sha256
    }
  }
  data = {
    "topology.json" = jsonencode({
      schema                                = "fs2-serve.nebius.ai/network-policy-boundary-topology/v1"
      mode                                  = var.network_policy_boundary.mode
      security_owner_username               = local.control_plane_network_policy_security_owner
      security_bootstrap_username           = local.control_plane_network_policy_security_bootstrap
      successor_security_owner_username     = local.control_plane_network_policy_successor_owner
      successor_security_bootstrap_username = local.control_plane_network_policy_successor_bootstrap
      security_handoff                      = local.control_plane_network_policy_security_handoff
      gateway_namespace                     = local.control_plane_network_policy_gateway_namespace
      controller_namespace                  = local.control_plane_network_policy_controller_namespace
      policy_names                          = local.control_plane_network_policy_names
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
          var.network_policy_boundary.security_handoff_peer_gid != null &&
          var.network_policy_boundary.identity_epoch != null &&
          var.network_policy_boundary.prior_identity_epoch != null &&
          var.network_policy_boundary.successor_identity_epoch != null &&
          var.network_policy_boundary.security_subject_inventory != null &&
          var.network_policy_boundary.security_subject_provider_snapshot_path != null &&
          var.network_policy_boundary.security_owner_kubeconfig_path != null &&
          var.network_policy_boundary.security_bootstrap_kubeconfig_path != null &&
          var.network_policy_boundary.prior_security_owner_kubeconfig_path != null &&
          var.network_policy_boundary.prior_security_bootstrap_kubeconfig_path != null &&
          var.network_policy_boundary.security_handoff_client_private_key_path != null &&
          var.network_policy_boundary.release_identity != null &&
          var.network_policy_boundary.security_owner_identity != null &&
          var.network_policy_boundary.security_bootstrap_identity != null &&
          var.network_policy_boundary.prior_security_owner_identity != null &&
          var.network_policy_boundary.prior_security_bootstrap_identity != null
        )
      )
      error_message = "Public NetworkPolicy boundaries require pinned keys, a signed complete subject inventory, a versioned identity epoch, dedicated runtime/bootstrap kubeconfigs, exact expiring whoami tuples, and the exact effective Unix peer UID/GID."
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
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_owner" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = "fs2-network-policy-security-owner"
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
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterroles", "clusterrolebindings"]
    resource_names = [
      "fs2-network-policy-security-owner",
      "fs2-network-policy-security-auditor",
    ]
    verbs = ["get"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_binding_v1" "control_plane_network_policy_security_owner" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = "fs2-network-policy-security-owner"
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
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_owner
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  # Epoch retirement is deliberately last for cluster-scoped authority. The
  # current bootstrap remains authorized by the prior epoch until admission and
  # both namespace bindings have moved to this epoch and its successor.
  depends_on = [
    kubernetes_manifest.control_plane_network_policy_boundary_admission_binding,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller,
  ]
}

resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_auditor" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = "fs2-network-policy-security-auditor"
    labels = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = [""]
    resources  = ["namespaces", "serviceaccounts"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["authorization.k8s.io"]
    resources  = ["subjectaccessreviews"]
    verbs      = ["create"]
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["roles", "clusterroles"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["certificates.k8s.io"]
    resources  = ["certificatesigningrequests"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterrolebindings"]
    resource_names = [
      "fs2-network-policy-security-owner",
      "fs2-network-policy-security-auditor",
      local.control_plane_network_policy_bootstrap_names.cluster,
    ]
    verbs = ["get"]
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
    api_groups     = ["coordination.k8s.io"]
    resources      = ["leases"]
    resource_names = [local.control_plane_network_policy_state_name]
    verbs          = ["get"]
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    resource_names = [
      local.control_plane_network_policy_names.proxy_guard,
      local.control_plane_network_policy_names.controller_guard,
      local.control_plane_network_policy_names.default_deny,
    ]
    verbs = ["get"]
  }
  rule {
    api_groups     = ["admissionregistration.k8s.io"]
    resources      = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
    resource_names = ["fs2-network-policy-boundary"]
    verbs          = ["get"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_binding_v1" "control_plane_network_policy_security_auditor" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = "fs2-network-policy-security-auditor"
    labels = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.control_plane_network_policy_security_auditor.metadata[0].name
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [kubernetes_manifest.control_plane_network_policy_boundary_admission_binding]
}

// Subject rotation is a separate capability. The read-only auditor cannot
// mutate bindings, and the runtime security owner cannot rotate its own
// subjects. This externally provisioned role is limited to the three exact
// cluster bindings; namespace-local companions below avoid a cluster-wide
// resourceNames grant for RoleBindings.
resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = local.control_plane_network_policy_bootstrap_names.cluster
    labels = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterrolebindings"]
    resource_names = [
      "fs2-network-policy-security-owner",
      "fs2-network-policy-security-auditor",
      local.control_plane_network_policy_bootstrap_names.cluster,
    ]
    verbs = ["get", "patch", "update"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_cluster_role_binding_v1" "control_plane_network_policy_security_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name   = local.control_plane_network_policy_bootstrap_names.cluster
    labels = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = local.control_plane_network_policy_bootstrap_names.cluster
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  // Rotate the bootstrap's own authority only after every binding it governs.
  depends_on = [
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner,
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_auditor,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state_bootstrap,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway_bootstrap,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller_bootstrap,
  ]
}

// All role definitions and initial bindings are installed by the external
// security authority and adopted at stable addresses. Terraform may rotate
// exact binding subjects, but it cannot create, replace, delete, or reconcile
// the immutable permission definitions with the short-lived bootstrap.
import {
  to = kubernetes_cluster_role_v1.control_plane_network_policy_security_owner
  id = "fs2-network-policy-security-owner"
}

import {
  to = kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner
  id = "fs2-network-policy-security-owner"
}

import {
  to = kubernetes_cluster_role_v1.control_plane_network_policy_security_auditor
  id = "fs2-network-policy-security-auditor"
}

import {
  to = kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_auditor
  id = "fs2-network-policy-security-auditor"
}

import {
  to = kubernetes_cluster_role_v1.control_plane_network_policy_security_bootstrap
  id = "fs2-network-policy-security-bootstrap"
}

import {
  to = kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_bootstrap
  id = "fs2-network-policy-security-bootstrap"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_state
  id = "fs2-system/fs2-network-policy-transition"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_state
  id = "fs2-system/fs2-network-policy-transition"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_state_bootstrap
  id = "fs2-system/fs2-network-policy-transition-bootstrap"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_state_bootstrap
  id = "fs2-system/fs2-network-policy-transition-bootstrap"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_gateway
  id = "${local.control_plane_network_policy_gateway_namespace}/fs2-network-policy-transition-gateway"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway
  id = "${local.control_plane_network_policy_gateway_namespace}/fs2-network-policy-transition-gateway"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_gateway_bootstrap
  id = "${local.control_plane_network_policy_gateway_namespace}/fs2-network-policy-transition-gateway-bootstrap"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway_bootstrap
  id = "${local.control_plane_network_policy_gateway_namespace}/fs2-network-policy-transition-gateway-bootstrap"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_controller
  id = "${local.control_plane_network_policy_controller_namespace}/fs2-network-policy-transition-controller"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_controller
  id = "${local.control_plane_network_policy_controller_namespace}/fs2-network-policy-transition-controller"
}

import {
  to = kubernetes_role_v1.control_plane_network_policy_transition_controller_bootstrap
  id = "${local.control_plane_network_policy_controller_namespace}/fs2-network-policy-transition-controller-bootstrap"
}

import {
  to = kubernetes_role_binding_v1.control_plane_network_policy_transition_controller_bootstrap
  id = "${local.control_plane_network_policy_controller_namespace}/fs2-network-policy-transition-controller-bootstrap"
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
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_owner
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  # Removing the current bootstrap from fs2-system is the final epoch-fencing
  # write. A retry uses the newly bound current owner or successor bootstrap.
  depends_on = [
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner,
    kubernetes_config_map_v1.control_plane_network_policy_topology,
  ]
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_state_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.state
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["rolebindings"]
    resource_names = [
      local.control_plane_network_policy_state_name,
      local.control_plane_network_policy_bootstrap_names.state,
    ]
    verbs = ["get", "patch", "update"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_state_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.state
    namespace = "fs2-system"
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = local.control_plane_network_policy_bootstrap_names.state
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [kubernetes_role_binding_v1.control_plane_network_policy_transition_state]
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
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

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
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_owner
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_manifest.control_plane_network_policy_boundary_admission_binding,
    kubernetes_network_policy_v1.control_plane_public_envoy_boundary,
    kubernetes_network_policy_v1.control_plane_envoy_default_deny,
  ]
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_gateway_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.gateway
    namespace = local.control_plane_network_policy_gateway_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["rolebindings"]
    resource_names = [
      "${local.control_plane_network_policy_state_name}-gateway",
      local.control_plane_network_policy_bootstrap_names.gateway,
    ]
    verbs = ["get", "patch", "update"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_gateway_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.gateway
    namespace = local.control_plane_network_policy_gateway_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = local.control_plane_network_policy_bootstrap_names.gateway
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway]
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
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

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
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_owner
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway_bootstrap,
    kubernetes_network_policy_v1.control_plane_envoy_controller_boundary,
  ]
}

resource "kubernetes_role_v1" "control_plane_network_policy_transition_controller_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.controller
    namespace = local.control_plane_network_policy_controller_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["rolebindings"]
    resource_names = [
      "${local.control_plane_network_policy_state_name}-controller",
      local.control_plane_network_policy_bootstrap_names.controller,
    ]
    verbs = ["get", "patch", "update"]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.control_plane_network_policy_security_owner_preflight]
}

resource "kubernetes_role_binding_v1" "control_plane_network_policy_transition_controller_bootstrap" {
  provider = kubernetes.network_policy_security_owner

  metadata {
    name      = local.control_plane_network_policy_bootstrap_names.controller
    namespace = local.control_plane_network_policy_controller_namespace
    labels    = local.control_plane_network_policy_boundary_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = local.control_plane_network_policy_bootstrap_names.controller
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_security_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }
  subject {
    kind      = "User"
    name      = local.control_plane_network_policy_successor_bootstrap
    api_group = "rbac.authorization.k8s.io"
  }

  lifecycle { prevent_destroy = true }

  depends_on = [kubernetes_role_binding_v1.control_plane_network_policy_transition_controller]
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
        resourceRules = [
          {
            apiGroups   = ["*"]
            apiVersions = ["*"]
            operations  = ["UPDATE", "DELETE"]
            resources   = ["*"]
            scope       = "*"
          },
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["UPDATE"]
            resources   = ["namespaces/finalize"]
            scope       = "Cluster"
          },
        ]
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
          expression = <<-CEL
            request.operation != 'UPDATE' ||
            oldObject.apiVersion != 'v1' ||
            oldObject.kind != 'ConfigMap' ||
            oldObject.metadata.name != '${local.control_plane_network_policy_topology_name}' ||
            (
              has(oldObject.metadata.annotations) &&
              'fs2.nebius.ai/network-policy-identity-epoch' in oldObject.metadata.annotations &&
              'fs2.nebius.ai/network-policy-credential-set-sha256' in oldObject.metadata.annotations &&
              has(object.metadata.annotations) &&
              'fs2.nebius.ai/network-policy-identity-epoch' in object.metadata.annotations &&
              'fs2.nebius.ai/network-policy-credential-set-sha256' in object.metadata.annotations &&
              (
                (
                  object.metadata.annotations['fs2.nebius.ai/network-policy-identity-epoch'] ==
                    oldObject.metadata.annotations['fs2.nebius.ai/network-policy-identity-epoch'] &&
                  object.metadata.annotations['fs2.nebius.ai/network-policy-credential-set-sha256'] ==
                    oldObject.metadata.annotations['fs2.nebius.ai/network-policy-credential-set-sha256'] &&
                  object.data == oldObject.data
                ) ||
                (
                  request.userInfo.username in [
                    '${local.control_plane_network_policy_security_bootstrap}',
                    '${local.control_plane_network_policy_successor_bootstrap}'
                  ] &&
                  object.metadata.annotations['fs2.nebius.ai/network-policy-identity-epoch'] !=
                    oldObject.metadata.annotations['fs2.nebius.ai/network-policy-identity-epoch']
                )
              )
            )
          CEL
          message    = "protected topology credential changes require a new bootstrap-owned identity epoch; same-epoch updates must preserve exact data"
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'DELETE'"
          message    = "permanent boundary objects are never deleted"
          reason     = "Forbidden"
        },
        {
          expression = <<-CEL
            request.operation != 'UPDATE' ||
            oldObject.apiVersion != 'rbac.authorization.k8s.io/v1' ||
            !(oldObject.kind in ['Role', 'ClusterRole'])
          CEL
          message    = "externally owned permanent Role and ClusterRole definitions are immutable"
          reason     = "Forbidden"
        },
        {
          expression = <<-CEL
            request.operation != 'UPDATE' ||
            oldObject.apiVersion != 'rbac.authorization.k8s.io/v1' ||
            !(oldObject.kind in ['RoleBinding', 'ClusterRoleBinding']) ||
            (
              request.userInfo.username in [
                '${local.control_plane_network_policy_security_bootstrap}',
                '${local.control_plane_network_policy_successor_bootstrap}'
              ] &&
              object.roleRef.apiGroup == 'rbac.authorization.k8s.io' &&
              object.roleRef.name == object.metadata.name &&
              (
                (
                  object.kind == 'ClusterRoleBinding' &&
                  object.roleRef.kind == 'ClusterRole' &&
                  object.metadata.name in [
                    'fs2-network-policy-security-owner',
                    'fs2-network-policy-security-auditor',
                    'fs2-network-policy-security-bootstrap'
                  ]
                ) ||
                (
                  object.kind == 'RoleBinding' &&
                  object.roleRef.kind == 'Role' &&
                  (
                    (object.metadata.namespace == 'fs2-system' && object.metadata.name in [
                      '${local.control_plane_network_policy_state_name}',
                      '${local.control_plane_network_policy_bootstrap_names.state}'
                    ]) ||
                    (object.metadata.namespace == '${local.control_plane_network_policy_gateway_namespace}' && object.metadata.name in [
                      '${local.control_plane_network_policy_state_name}-gateway',
                      '${local.control_plane_network_policy_bootstrap_names.gateway}'
                    ]) ||
                    (object.metadata.namespace == '${local.control_plane_network_policy_controller_namespace}' && object.metadata.name in [
                      '${local.control_plane_network_policy_state_name}-controller',
                      '${local.control_plane_network_policy_bootstrap_names.controller}'
                    ])
                  )
                )
              ) &&
              (
                (
                  object.metadata.name in [
                    'fs2-network-policy-security-owner',
                    '${local.control_plane_network_policy_state_name}',
                    '${local.control_plane_network_policy_state_name}-gateway',
                    '${local.control_plane_network_policy_state_name}-controller'
                  ] &&
                  size(object.subjects) == 3 &&
                  object.subjects.all(subject,
                    subject.apiGroup == 'rbac.authorization.k8s.io' &&
                    subject.kind == 'User' &&
                    subject.name in [
                      '${local.control_plane_network_policy_security_owner}',
                      '${local.control_plane_network_policy_successor_owner}',
                      '${local.control_plane_network_policy_successor_bootstrap}'
                    ]) &&
                  [
                    '${local.control_plane_network_policy_security_owner}',
                    '${local.control_plane_network_policy_successor_owner}',
                    '${local.control_plane_network_policy_successor_bootstrap}'
                  ].all(name, object.subjects.exists(subject, subject.name == name))
                ) ||
                (
                  object.metadata.name in [
                    'fs2-network-policy-security-auditor',
                    'fs2-network-policy-security-bootstrap',
                    '${local.control_plane_network_policy_bootstrap_names.state}',
                    '${local.control_plane_network_policy_bootstrap_names.gateway}',
                    '${local.control_plane_network_policy_bootstrap_names.controller}'
                  ] &&
                  size(object.subjects) == 2 &&
                  object.subjects.all(subject,
                    subject.apiGroup == 'rbac.authorization.k8s.io' &&
                    subject.kind == 'User' &&
                    subject.name in [
                      '${local.control_plane_network_policy_security_bootstrap}',
                      '${local.control_plane_network_policy_successor_bootstrap}'
                    ]) &&
                  [
                    '${local.control_plane_network_policy_security_bootstrap}',
                    '${local.control_plane_network_policy_successor_bootstrap}'
                  ].all(name, object.subjects.exists(subject, subject.name == name))
                )
              )
            )
          CEL
          message    = "permanent RBAC bindings require the exact next-epoch role and user-only subject set"
          reason     = "Forbidden"
        },
        {
          expression = <<-CEL
            request.operation != 'UPDATE' || (
              has(object.metadata.labels) &&
              object.metadata.labels['fs2.nebius.ai/network-policy-boundary'] == 'permanent' &&
              request.userInfo.username in [
                '${local.control_plane_network_policy_security_owner}',
                '${local.control_plane_network_policy_security_bootstrap}',
                '${local.control_plane_network_policy_successor_bootstrap}'
              ] &&
              (!('fs2.nebius.ai/network-policy-role' in oldObject.metadata.labels) ||
               ('fs2.nebius.ai/network-policy-role' in object.metadata.labels &&
                object.metadata.labels['fs2.nebius.ai/network-policy-role'] == oldObject.metadata.labels['fs2.nebius.ai/network-policy-role']))
            )
          CEL
          message    = "permanent boundary updates require the current runtime owner or an exact preauthorized bootstrap epoch and immutable ownership labels"
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
    kubernetes_config_map_v1.control_plane_network_policy_transition_receipt,
    kubernetes_manifest.control_plane_network_policy_transition_lease,
    kubernetes_config_map_v1.control_plane_network_policy_topology,
    kubernetes_config_map_v1.control_plane_network_policy_boundary_parameters,
  ]
}

// This data source is deliberately apply-deferred behind every epoch-bearing
// binding. Pre-apply proof requires the still-current prior owner to retain its
// narrow authority; only this postflight may attest that the non-destructive
// subject updates retired both prior credentials.
data "external" "control_plane_network_policy_epoch_retirement" {
  program = [
    "python3",
    "${path.module}/scripts/verify-network-policy-epoch-retirement.py",
  ]
  query = {
    mode                                = var.network_policy_boundary.mode
    context                             = var.kube_context
    kube_system_uid                     = var.kube_system_uid
    identity_epoch                      = coalesce(var.network_policy_boundary.identity_epoch, "unconfigured")
    prior_identity_epoch                = coalesce(var.network_policy_boundary.prior_identity_epoch, "unconfigured-prior")
    successor_identity_epoch            = coalesce(var.network_policy_boundary.successor_identity_epoch, "unconfigured-successor")
    preflight_sha256                    = data.external.control_plane_network_policy_security_preflight_v2.result.contract_sha256
    current_owner_kubeconfig            = abspath(local.control_plane_network_policy_security_owner_kubeconfig_path)
    current_bootstrap_kubeconfig        = abspath(local.control_plane_network_policy_security_bootstrap_kubeconfig_path)
    prior_owner_kubeconfig              = abspath(local.control_plane_network_policy_prior_security_owner_kubeconfig_path)
    prior_bootstrap_kubeconfig          = abspath(local.control_plane_network_policy_prior_security_bootstrap_kubeconfig_path)
    current_owner_username              = local.control_plane_network_policy_security_owner
    current_bootstrap_username          = local.control_plane_network_policy_security_bootstrap
    prior_owner_username                = local.control_plane_network_policy_prior_security_owner
    prior_bootstrap_username            = local.control_plane_network_policy_prior_bootstrap
    successor_owner_username            = local.control_plane_network_policy_successor_owner
    successor_bootstrap_username        = local.control_plane_network_policy_successor_bootstrap
    current_owner_user_info_sha256      = coalesce(local.control_plane_network_policy_security_handoff.identity_boundary.security_user_info_sha256, "unconfigured")
    current_bootstrap_user_info_sha256  = coalesce(local.control_plane_network_policy_security_handoff.identity_boundary.bootstrap_user_info_sha256, "unconfigured")
    prior_owner_user_info_sha256        = coalesce(local.control_plane_network_policy_security_handoff.identity_boundary.prior_security_user_info_sha256, "unconfigured")
    prior_bootstrap_user_info_sha256    = coalesce(local.control_plane_network_policy_security_handoff.identity_boundary.prior_bootstrap_user_info_sha256, "unconfigured")
    current_owner_kubeconfig_sha256     = data.external.control_plane_network_policy_security_preflight_v2.result.security_kubeconfig_sha256
    current_bootstrap_kubeconfig_sha256 = data.external.control_plane_network_policy_security_preflight_v2.result.bootstrap_kubeconfig_sha256
    prior_owner_kubeconfig_sha256       = data.external.control_plane_network_policy_security_preflight_v2.result.prior_security_kubeconfig_sha256
    prior_bootstrap_kubeconfig_sha256   = data.external.control_plane_network_policy_security_preflight_v2.result.prior_bootstrap_kubeconfig_sha256
    external_role_bundle_sha256         = data.external.control_plane_network_policy_security_preflight_v2.result.external_role_bundle_sha256
    gateway_namespace                   = local.control_plane_network_policy_gateway_namespace
    controller_namespace                = local.control_plane_network_policy_controller_namespace
  }

  depends_on = [
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner,
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_auditor,
    kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_bootstrap,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_state_bootstrap,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_gateway_bootstrap,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller,
    kubernetes_role_binding_v1.control_plane_network_policy_transition_controller_bootstrap,
    kubernetes_manifest.control_plane_network_policy_boundary_admission,
    kubernetes_manifest.control_plane_network_policy_boundary_admission_binding,
  ]

  lifecycle {
    postcondition {
      condition = (
        self.result.verified == "true" &&
        can(regex("^[0-9a-f]{64}$", self.result.contract_sha256))
      )
      error_message = "The apply-deferred epoch-retirement proof did not complete exactly."
    }
  }
}
