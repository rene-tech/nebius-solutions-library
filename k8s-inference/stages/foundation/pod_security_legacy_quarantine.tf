/*
REJECTED PLATFORM-OWNED LEGACY ADOPTION (retained as source provenance).

Exact UID/resourceVersion/spec-bound adoption now belongs only to the external
custody root. This inactive block documents the rejected in-process design.

# The signed v4 baseline supplies the exact existing identities. These import
# blocks adopt them in place; no object is deleted, recreated, or renamed.
# Admission has already frozen the identities before this phase and permits
# only the exact custody-owner transition rendered below.
resource "kubernetes_manifest" "pod_security_legacy_networkpolicy_quarantine" {
  provider = kubernetes.pod_security_custody
  for_each = local.pod_security_legacy_networkpolicies

  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = each.key
      namespace = "fs2-models"
      labels = {
        "security.fs2.nebius.ai/retained-quarantine" = "true"
      }
      annotations = {
        "security.fs2.nebius.ai/baseline-uid"           = each.value.uid
        "security.fs2.nebius.ai/baseline-object-sha256" = each.value.object_sha256
      }
    }
    spec = {
      podSelector = {
        matchLabels = {
          "security.fs2.nebius.ai/retained-quarantine" = "true"
        }
      }
      policyTypes = ["Ingress", "Egress"]
      ingress     = []
      egress      = []
    }
  }

  field_manager {
    name            = "fs2-sai07-retained-quarantine"
    force_conflicts = false
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        each.value.api_version == "networking.k8s.io/v1" &&
        each.value.namespace == "fs2-models" &&
        each.value.uid != "" &&
        can(regex("^[a-f0-9]{64}$", each.value.object_sha256))
      )
      error_message = "Legacy NetworkPolicy adoption must be bound to one exact signed baseline identity."
    }
  }

  depends_on = [
    kubernetes_manifest.pod_security_legacy_cleanup_fence_binding,
    module.pod_security_rollout_gate,
  ]
}

import {
  for_each = local.pod_security_legacy_networkpolicies
  to       = kubernetes_manifest.pod_security_legacy_networkpolicy_quarantine[each.key]
  id       = "apiVersion=networking.k8s.io/v1,kind=NetworkPolicy,namespace=fs2-models,name=${each.key}"
}

resource "kubernetes_manifest" "pod_security_legacy_serviceaccount_quarantine" {
  provider = kubernetes.pod_security_custody
  for_each = local.pod_security_legacy_serviceaccounts

  manifest = {
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata = {
      name      = each.key
      namespace = "fs2-models"
      labels = {
        "security.fs2.nebius.ai/retained-quarantine" = "true"
      }
      annotations = {
        "security.fs2.nebius.ai/baseline-uid"           = each.value.uid
        "security.fs2.nebius.ai/baseline-object-sha256" = each.value.object_sha256
      }
    }
    automountServiceAccountToken = false
    imagePullSecrets             = []
    secrets                      = []
  }

  field_manager {
    name            = "fs2-sai07-retained-quarantine"
    force_conflicts = false
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        each.value.api_version == "v1" &&
        each.value.namespace == "fs2-models" &&
        each.value.uid != "" &&
        can(regex("^[a-f0-9]{64}$", each.value.object_sha256))
      )
      error_message = "Legacy ServiceAccount adoption must be bound to one exact signed baseline identity."
    }
  }

  depends_on = [
    kubernetes_manifest.pod_security_legacy_cleanup_fence_binding,
    module.pod_security_rollout_gate,
  ]
}

import {
  for_each = local.pod_security_legacy_serviceaccounts
  to       = kubernetes_manifest.pod_security_legacy_serviceaccount_quarantine[each.key]
  id       = "apiVersion=v1,kind=ServiceAccount,namespace=fs2-models,name=${each.key}"
}

# DaemonSets cannot be scaled to zero, and deleting their Pods is forbidden by
# the active operating constraint.  Terraform therefore adopts only one
# additive quarantine label.  The custody admission fence simultaneously
# freezes each exact DaemonSet spec and rejects every future owned Pod. Existing
# Pods are retained; enforcement remains blocked until they naturally reach
# zero and the read-only result proves all status counters and owned-Pod reads
# are empty.
resource "kubernetes_labels" "pod_security_legacy_daemonset_quarantine" {
  provider = kubernetes.pod_security_custody
  for_each = local.pod_security_legacy_daemonsets

  api_version = "apps/v1"
  kind        = "DaemonSet"
  metadata {
    name      = each.key
    namespace = "fs2-models"
  }
  labels = {
    "security.fs2.nebius.ai/retained-quarantine"    = "true"
    "security.fs2.nebius.ai/baseline-uid"           = each.value.uid
    "security.fs2.nebius.ai/baseline-object-sha256" = each.value.object_sha256
  }
  force = false

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        each.value.api_version == "apps/v1" &&
        each.value.namespace == "fs2-models" &&
        each.value.uid != "" &&
        can(regex("^[a-f0-9]{64}$", each.value.object_sha256))
      )
      error_message = "Legacy DaemonSet quarantine ownership must be bound to one exact signed baseline identity."
    }
  }

  depends_on = [
    kubernetes_manifest.pod_security_legacy_cleanup_fence_binding,
    module.pod_security_rollout_gate,
  ]
}
*/
