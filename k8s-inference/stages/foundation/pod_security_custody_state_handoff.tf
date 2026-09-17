# Non-destructive state handoff to stages/pod-security-custody.
#
# The independently signed external handoff consumed by
# module.pod_security_rollout_gate must attest that every live object below was
# adopted with the same UID/resourceVersion/spec hash before this root runs.
# `destroy = false` removes only the old Terraform address; it never deletes or
# replaces the Kubernetes object. The rollout contract makes a missing external
# handoff a hard planning error.

removed {
  from = kubernetes_manifest.pod_security_custody_boundary_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_custody_boundary_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_config_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_config_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_pod_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_pod_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_daemonset_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.node_observability_daemonset_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_service_account_v1.pod_security_rollout_manager
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_service_account_v1.pod_security_rollout_custodian
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_cluster_role_v1.pod_security_rollout_reader
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_cluster_role_binding_v1.pod_security_rollout_reader
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_cluster_role_binding_v1.pod_security_rollout_custodian_reader
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_role_v1.pod_security_rollout_ledger
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_role_binding_v1.pod_security_rollout_ledger
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_role_binding_v1.pod_security_rollout_custodian_ledger
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_cluster_role_v1.pod_security_external_custody_audit
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_cluster_role_binding_v1.pod_security_external_custody_audit
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_role_v1.pod_security_rollout_token_request
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_role_binding_v1.pod_security_rollout_token_request
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_rollout_token_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_rollout_token_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_enforcement_fence_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_enforcement_fence_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_legacy_cleanup_fence_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_legacy_cleanup_fence_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_ledger_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_ledger_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.snapshot_pod_policy
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.snapshot_pod_binding
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_legacy_networkpolicy_quarantine
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_manifest.pod_security_legacy_serviceaccount_quarantine
  lifecycle { destroy = false }
}

removed {
  from = kubernetes_labels.pod_security_legacy_daemonset_quarantine
  lifecycle { destroy = false }
}
