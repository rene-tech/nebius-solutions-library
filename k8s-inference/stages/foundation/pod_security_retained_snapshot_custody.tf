# Retain the two predecessor snapshot admission objects at their original
# count-indexed Terraform addresses. The historical definitions remain in the
# commented design record in pod_security_snapshot_admission.tf; these active
# declarations prevent a platform plan from interpreting that archival move as
# authorization to destroy or forget the live objects. External custody may
# inspect them, but platform state remains their sole Terraform owner.
resource "kubernetes_manifest" "snapshot_pod_policy" {
  provider = kubernetes.pod_security_custody
  count    = local.node_observability_exception_enabled ? 1 : 0

  lifecycle {
    prevent_destroy = true
  }

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-snapshot-exact-profile"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods"]
          },
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods/ephemeralcontainers"]
          },
        ]
      }
      validations = [
        {
          expression = "request.userInfo.username in ['${local.snapshot_manager_username}','${local.snapshot_job_controller}']"
          message    = "Only the fixed snapshot manager or Kubernetes Job controller may create exact-profile snapshot Pods."
        },
        {
          expression = "(request.userInfo.username == '${local.snapshot_manager_username}' && (${local.snapshot_fixed_pod_expression})) || (request.userInfo.username == '${local.snapshot_job_controller}' && ((${local.snapshot_durability_pod_expression}) || (${local.snapshot_reference_probe_pod_expression})))"
          message    = "Snapshot Pods must match the exact runtime profile or one of the two exact retained-storage proof profiles."
        },
      ]
    }
  }

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    terraform_data.cluster_contract,
  ]
}

resource "kubernetes_manifest" "snapshot_pod_binding" {
  provider = kubernetes.pod_security_custody
  count    = local.node_observability_exception_enabled ? 1 : 0

  lifecycle {
    prevent_destroy = true
  }

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-snapshot-exact-profile"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-snapshot-exact-profile"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "security.fs2.nebius.ai/snapshot-only" = "true"
          }
        }
      }
    }
  }

  depends_on = [kubernetes_manifest.snapshot_pod_policy]
}
