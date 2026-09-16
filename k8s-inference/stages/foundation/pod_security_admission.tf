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
  pod_security_rollout_manager_username = "system:serviceaccount:fs2-system:fs2-pod-security-rollout-manager"
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
  pod_security_exception_exact_daemonset_expressions = {
    fs2-node-exporter                    = format("object.metadata.name == 'fs2-node-exporter' && object.spec.template.spec.serviceAccountName == 'fs2-node-exporter' && object.spec.template.spec.automountServiceAccountToken == false && object.spec.template.spec.hostNetwork == true && object.spec.template.spec.hostPID == true && object.spec.template.spec.hostIPC == false && object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'node-exporter' && object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command) && object.spec.template.spec.containers[0].args == ['--path.procfs=/host/proc','--path.sysfs=/host/sys','--path.rootfs=/host/root','--path.udev.data=/host/root/run/udev/data','--web.listen-address=[$(HOST_IP)]:9100'] && object.spec.template.spec.volumes.filter(v, has(v.hostPath)).size() == 3 && object.spec.template.spec.volumes.exists(v, v.name == 'proc' && v.hostPath.path == '/proc') && object.spec.template.spec.volumes.exists(v, v.name == 'sys' && v.hostPath.path == '/sys') && object.spec.template.spec.volumes.exists(v, v.name == 'root' && v.hostPath.path == '/') && object.spec.template.spec.containers[0].volumeMounts.filter(m, m.name in ['proc','sys','root']).all(m, m.readOnly == true)", var.pod_security_host_agent_images["node-exporter"])
    fs2-otel-node-agent                  = format("object.metadata.name == 'fs2-otel-node-agent' && object.spec.template.spec.serviceAccountName == 'fs2-otel-node' && object.spec.template.spec.automountServiceAccountToken == true && object.spec.template.spec.hostNetwork == false && object.spec.template.spec.hostPID == false && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false) && object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'opentelemetry-collector' && object.spec.template.spec.containers[0].image == '%s' && object.spec.template.spec.containers[0].command == ['/otelcol-k8s'] && object.spec.template.spec.containers[0].args == ['--config=/conf/relay.yaml'] && object.spec.template.spec.volumes.filter(v, has(v.hostPath)).size() == 2 && object.spec.template.spec.volumes.exists(v, v.name == 'varlogpods' && v.hostPath.path == '/var/log/pods') && object.spec.template.spec.volumes.exists(v, v.name == 'varlibdockercontainers' && v.hostPath.path == '/var/lib/docker/containers') && object.spec.template.spec.containers[0].volumeMounts.filter(m, m.name in ['varlogpods','varlibdockercontainers']).all(m, m.readOnly == true)", var.pod_security_host_agent_images["otel-node"])
    fs2-dcgm-exporter                    = format("object.metadata.name == 'fs2-dcgm-exporter' && object.spec.template.spec.serviceAccountName == 'fs2-dcgm-exporter' && object.spec.template.spec.automountServiceAccountToken == true && object.spec.template.spec.hostPID == false && (!has(object.spec.template.spec.hostNetwork) || object.spec.template.spec.hostNetwork == false) && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false) && object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'exporter' && object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command) && (!has(object.spec.template.spec.containers[0].args) || object.spec.template.spec.containers[0].args == [] || object.spec.template.spec.containers[0].args == ['--collect-interval=5000']) && object.spec.template.spec.volumes.filter(v, has(v.hostPath)).size() == 1 && object.spec.template.spec.volumes.exists(v, v.name == 'pod-gpu-resources' && v.hostPath.path == '/var/lib/kubelet/pod-resources') && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'pod-gpu-resources' && m.readOnly == true)", var.pod_security_host_agent_images["dcgm-exporter"])
    fs2-serve-control-plane-gpu-observer = format("object.metadata.name == 'fs2-serve-control-plane-gpu-observer' && object.spec.template.spec.serviceAccountName == 'fs2-serve-control-plane-gpu-observer' && object.spec.template.spec.automountServiceAccountToken == false && (!has(object.spec.template.spec.hostNetwork) || object.spec.template.spec.hostNetwork == false) && (!has(object.spec.template.spec.hostPID) || object.spec.template.spec.hostPID == false) && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false) && object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'observer' && object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command) && object.spec.template.spec.containers[0].args == ['gpu-allocation-observer'] && object.spec.template.spec.volumes.filter(v, has(v.hostPath)).size() == 1 && object.spec.template.spec.volumes.exists(v, v.name == 'kubelet-device-plugins' && v.hostPath.path == '/var/lib/kubelet/device-plugins') && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'kubelet-device-plugins' && m.readOnly == true)", var.pod_security_host_agent_images["gpu-observer"])
  }
  pod_security_exception_exact_daemonset_expression = join(
    " || ",
    [for _, expression in local.pod_security_exception_exact_daemonset_expressions : "(${expression})"],
  )
  pod_security_exception_exact_pod_expression = join(" || ", [
    for daemonset, expression in local.pod_security_exception_exact_daemonset_expressions : format(
      "(%s)",
      replace(
        replace(
          expression,
          "object.spec.template.spec",
          "object.spec",
        ),
        "object.metadata.name == '${daemonset}'",
        "object.metadata.ownerReferences.exists(o, o.apiVersion == 'apps/v1' && o.kind == 'DaemonSet' && o.name == '${daemonset}' && o.controller == true)",
      ),
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
          expression = local.pod_security_exception_exact_pod_expression
          message    = "Host-agent Pods must match the exact reviewed image, command, service account, host namespace, and read-only host-path contract."
        },
        {
          expression = "object.spec.containers.all(c, has(c.securityContext) && (!has(c.securityContext.privileged) || !c.securityContext.privileged) && (!has(c.securityContext.capabilities) || !has(c.securityContext.capabilities.add) || c.securityContext.capabilities.add.size() == 0 || (object.metadata.ownerReferences.exists(o, o.kind == 'DaemonSet' && o.name == 'fs2-dcgm-exporter') && c.name == 'exporter' && c.securityContext.capabilities.add == ['SYS_ADMIN']))) && (!has(object.spec.initContainers) || object.spec.initContainers.size() == 0)"
          message    = "Only the exact DCGM exporter container may add SYS_ADMIN; privileged and init containers are forbidden."
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
          expression = local.pod_security_exception_exact_daemonset_expression
          message    = "Host-agent DaemonSets must match the exact reviewed image, command, service account, host namespace, and read-only host-path contract."
        },
        {
          expression = "object.spec.template.spec.containers.all(c, has(c.securityContext) && (!has(c.securityContext.privileged) || !c.securityContext.privileged) && (!has(c.securityContext.capabilities) || !has(c.securityContext.capabilities.add) || c.securityContext.capabilities.add.size() == 0 || (object.metadata.name == 'fs2-dcgm-exporter' && c.name == 'exporter' && c.securityContext.capabilities.add == ['SYS_ADMIN']))) && (!has(object.spec.template.spec.initContainers) || object.spec.template.spec.initContainers.size() == 0)"
          message    = "Only the exact DCGM exporter container may add SYS_ADMIN; privileged and init containers are forbidden."
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

# Receipt consumption uses one Terraform-owned, tokenless identity. The caller
# may request a short-lived TokenRequest, but the API server sees this exact
# service account for every live read and monotonic ledger update. No tfvars
# username can become rollout authority.
resource "kubernetes_service_account_v1" "pod_security_rollout_manager" {
  metadata {
    name      = "fs2-pod-security-rollout-manager"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  automount_service_account_token = false

  depends_on = [kubernetes_namespace_v1.platform]
}

resource "kubernetes_cluster_role_v1" "pod_security_rollout_reader" {
  metadata {
    name   = "fs2-pod-security-rollout-reader"
    labels = local.common_labels
  }

  rule {
    api_groups = [""]
    resources = [
      "configmaps",
      "namespaces",
      "persistentvolumeclaims",
      "pods",
      "replicationcontrollers",
      "serviceaccounts",
    ]
    verbs = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["daemonsets", "deployments", "replicasets", "statefulsets"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["batch"]
    resources  = ["cronjobs", "jobs"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["storageclasses"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["admissionregistration.k8s.io"]
    resources  = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["jobset.x-k8s.io"]
    resources  = ["jobsets"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["inference.fs2.nebius.ai"]
    resources  = ["modeldeployments"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["keda.sh"]
    resources  = ["scaledobjects"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "pod_security_rollout_reader" {
  metadata {
    name   = "fs2-pod-security-rollout-reader"
    labels = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.pod_security_rollout_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.pod_security_rollout_manager.metadata[0].name
    namespace = kubernetes_service_account_v1.pod_security_rollout_manager.metadata[0].namespace
  }
}

resource "kubernetes_role_v1" "pod_security_rollout_ledger" {
  metadata {
    name      = "fs2-pod-security-rollout-ledger"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["fs2-pod-security-rollout-ledger"]
    verbs          = ["get", "update"]
  }
}

resource "kubernetes_role_binding_v1" "pod_security_rollout_ledger" {
  metadata {
    name      = "fs2-pod-security-rollout-ledger"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.pod_security_rollout_ledger.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.pod_security_rollout_manager.metadata[0].name
    namespace = kubernetes_service_account_v1.pod_security_rollout_manager.metadata[0].namespace
  }
}

# The rollout verifier is the only component that advances this ConfigMap and
# does so with a resourceVersion compare-and-swap. Admission makes the ledger
# non-deletable and restricts updates to the exact reviewed rollout identities;
# broad namespace RBAC cannot reset or replace the monotonic history.
resource "kubernetes_manifest" "pod_security_ledger_policy" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-pod-security-rollout-ledger"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["configmaps"]
        }]
      }
      matchConditions = [{
        name       = "exact-ledger"
        expression = "object.metadata.name == 'fs2-pod-security-rollout-ledger' || oldObject.metadata.name == 'fs2-pod-security-rollout-ledger'"
      }]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "The monotonic pod-security rollout ledger may not be deleted."
        },
        {
          expression = "request.userInfo.username == '${local.pod_security_rollout_manager_username}'"
          message    = "Only the Terraform-owned rollout-manager service account may advance the pod-security ledger."
        },
        {
          expression = "object.metadata.uid == oldObject.metadata.uid && object.metadata.name == oldObject.metadata.name && object.metadata.namespace == oldObject.metadata.namespace"
          message    = "The rollout ledger identity is immutable."
        },
        {
          expression = "object.data.size() == 1 && 'ledger.json' in object.data"
          message    = "The rollout ledger must contain only ledger.json."
        },
      ]
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "pod_security_ledger_binding" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-pod-security-rollout-ledger"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-pod-security-rollout-ledger"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_manifest.pod_security_ledger_policy,
  ]
}
