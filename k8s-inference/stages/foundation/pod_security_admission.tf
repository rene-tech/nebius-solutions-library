locals {
  pod_security_exception_daemonsets = [
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node-agent",
    "fs2-serve-control-plane-gpu-observer",
  ]
  pod_security_exception_service_accounts = [
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node",
    "fs2-serve-control-plane-gpu-observer",
  ]
  pod_security_exception_host_paths = [
    "/",
    "/proc",
    "/sys",
    "/var/lib/docker/containers",
    "/var/lib/kubelet/device-plugins",
    "/var/lib/kubelet/pod-resources",
    "/var/log/pods",
  ]
  pod_security_exception_owner_pairs = [
    ["fs2-dcgm-exporter", "fs2-dcgm-exporter"],
    ["fs2-node-exporter", "fs2-node-exporter"],
    ["fs2-otel-node-agent", "fs2-otel-node"],
    ["fs2-serve-control-plane-gpu-observer", "fs2-serve-control-plane-gpu-observer"],
  ]
  pod_security_exception_owner_expression = join(" || ", [
    for pair in local.pod_security_exception_owner_pairs : format(
      "(object.metadata.ownerReferences.exists(o, o.kind == 'DaemonSet' && o.name == '%s') && object.spec.serviceAccountName == '%s')",
      pair[0],
      pair[1],
    )
  ])
  pod_security_exception_manager_expression = format(
    "request.userInfo.username in %s",
    jsonencode(sort(tolist(var.pod_security_exception_manager_usernames))),
  )
  pod_security_daemonset_controller_expression = format(
    "request.userInfo.username in %s",
    jsonencode([
      "system:kube-controller-manager",
      "system:serviceaccount:kube-system:daemon-set-controller",
    ]),
  )
  pod_security_exception_daemonset_expression = join(" || ", [
    for pair in local.pod_security_exception_owner_pairs : format(
      "(object.metadata.name == '%s' && object.spec.template.spec.serviceAccountName == '%s')",
      pair[0],
      pair[1],
    )
  ])
}

# PSA must be privileged for the reviewed node integrations, so native
# ValidatingAdmissionPolicy supplies the namespace's enforceable exclusivity:
# only four exact DaemonSets may create Pods, only their exact SAs may run, and
# host namespaces/capabilities/paths remain bounded to the reviewed contracts.
resource "kubernetes_manifest" "node_observability_pod_policy" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-node-observability-pods"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE"]
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
          expression = local.pod_security_daemonset_controller_expression
          message    = "Only the Kubernetes DaemonSet controller may create host-agent Pods."
        },
        {
          expression = local.pod_security_exception_owner_expression
          message    = "Only the reviewed host-agent DaemonSets and service accounts may create Pods in this namespace."
        },
        {
          expression = "!has(object.spec.hostNetwork) || !object.spec.hostNetwork || object.metadata.ownerReferences.exists(o, o.kind == 'DaemonSet' && o.name == 'fs2-node-exporter')"
          message    = "Only the reviewed node exporter may use the host network."
        },
        {
          expression = "!has(object.spec.hostPID) || !object.spec.hostPID || object.metadata.ownerReferences.exists(o, o.kind == 'DaemonSet' && o.name == 'fs2-node-exporter')"
          message    = "Only the reviewed node exporter may use the host PID namespace."
        },
        {
          expression = format("object.spec.volumes.filter(v, has(v.hostPath)).all(v, v.hostPath.path in %s)", jsonencode(local.pod_security_exception_host_paths))
          message    = "Host paths are limited to the exact reviewed node-agent paths."
        },
        {
          expression = "object.spec.containers.all(c, (!has(c.securityContext) || !has(c.securityContext.privileged) || !c.securityContext.privileged) && (!has(c.securityContext) || !has(c.securityContext.capabilities) || !has(c.securityContext.capabilities.add) || c.securityContext.capabilities.add.all(cap, cap == 'SYS_ADMIN'))) && (!has(object.spec.initContainers) || object.spec.initContainers.all(c, (!has(c.securityContext) || !has(c.securityContext.privileged) || !c.securityContext.privileged) && (!has(c.securityContext) || !has(c.securityContext.capabilities) || !has(c.securityContext.capabilities.add) || c.securityContext.capabilities.add.all(cap, cap == 'SYS_ADMIN'))))"
          message    = "Containers may not be privileged and may add only the reviewed DCGM SYS_ADMIN capability."
        },
        {
          expression = "!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.size() == 0"
          message    = "Ephemeral containers are forbidden in the host-agent exception namespace."
        },
      ]
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "node_observability_pod_binding" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-node-observability-pods"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-node-observability-pods"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "security.fs2.nebius.ai/host-agent-only" = "true"
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_manifest.node_observability_pod_policy,
    kubernetes_namespace_v1.platform,
  ]
}

resource "kubernetes_manifest" "node_observability_daemonset_policy" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-node-observability-daemonsets"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["daemonsets"]
        }]
      }
      validations = [
        {
          expression = local.pod_security_exception_manager_expression
          message    = "Only an explicitly reviewed rollout identity may create or update host-agent DaemonSets."
        },
        {
          expression = local.pod_security_exception_daemonset_expression
          message    = "Only the four exact reviewed host-agent DaemonSets may exist in this namespace."
        },
        {
          expression = format("object.spec.template.spec.volumes.filter(v, has(v.hostPath)).all(v, v.hostPath.path in %s)", jsonencode(local.pod_security_exception_host_paths))
          message    = "DaemonSet host paths are limited to the exact reviewed node-agent paths."
        },
      ]
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "node_observability_daemonset_binding" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-node-observability-daemonsets"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-node-observability-daemonsets"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "security.fs2.nebius.ai/host-agent-only" = "true"
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_manifest.node_observability_daemonset_policy,
    kubernetes_namespace_v1.platform,
  ]
}
