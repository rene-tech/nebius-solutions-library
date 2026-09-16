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
  # ServiceAccount admission adds one randomly named projected API token
  # volume to the OTel/DCGM Pods after their DaemonSet templates are admitted.
  # Accept only that exact projection shape; a misleading kube-api-access-*
  # name must never hide a Secret or arbitrary projected source from the
  # bounded-volume checks below.
  pod_security_projected_api_token_expression = join(" && ", [
    "object.spec.template.spec.volumes.filter(v, v.name.startsWith('kube-api-access-')).size() <= 1",
    "object.spec.template.spec.volumes.filter(v, v.name.startsWith('kube-api-access-')).all(v, has(v.projected) && v.projected.defaultMode == 420 && v.projected.sources.size() == 3)",
    "object.spec.template.spec.volumes.filter(v, v.name.startsWith('kube-api-access-')).all(v, v.projected.sources.exists(s, has(s.serviceAccountToken) && s.serviceAccountToken.path == 'token' && s.serviceAccountToken.expirationSeconds >= 600 && s.serviceAccountToken.expirationSeconds <= 7200))",
    "object.spec.template.spec.volumes.filter(v, v.name.startsWith('kube-api-access-')).all(v, v.projected.sources.exists(s, has(s.configMap) && s.configMap.name == 'kube-root-ca.crt' && s.configMap.items == [{'key':'ca.crt','path':'ca.crt'}]))",
    "object.spec.template.spec.volumes.filter(v, v.name.startsWith('kube-api-access-')).all(v, v.projected.sources.exists(s, has(s.downwardAPI) && s.downwardAPI.items == [{'path':'namespace','fieldRef':{'apiVersion':'v1','fieldPath':'metadata.namespace'}}]))",
  ])
  pod_security_exception_exact_daemonset_expressions = {
    fs2-node-exporter = format(join(" && ", [
      "object.metadata.name == 'fs2-node-exporter'",
      "object.spec.template.spec.serviceAccountName == 'fs2-node-exporter' && object.spec.template.spec.automountServiceAccountToken == false",
      "object.spec.template.spec.hostNetwork == true && object.spec.template.spec.hostPID == true && object.spec.template.spec.hostIPC == false",
      "object.spec.template.spec.securityContext == {'fsGroup':65534,'runAsGroup':65534,'runAsNonRoot':true,'runAsUser':65534,'seccompProfile':{'type':'RuntimeDefault'}}",
      "object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'node-exporter'",
      "object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command)",
      "object.spec.template.spec.containers[0].args == ['--path.procfs=/host/proc','--path.sysfs=/host/sys','--path.rootfs=/host/root','--path.udev.data=/host/root/run/udev/data','--web.listen-address=[$(HOST_IP)]:9100']",
      "object.spec.template.spec.containers[0].env == [{'name':'HOST_IP','value':'0.0.0.0'}]",
      "object.spec.template.spec.containers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':true,'runAsNonRoot':true,'runAsUser':65534,'runAsGroup':65534,'seccompProfile':{'type':'RuntimeDefault'}}",
      "object.spec.template.spec.volumes.size() == 3 && object.spec.template.spec.volumes.all(v, has(v.hostPath))",
      "object.spec.template.spec.volumes.exists(v, v.name == 'proc' && v.hostPath == {'path':'/proc'}) && object.spec.template.spec.volumes.exists(v, v.name == 'sys' && v.hostPath == {'path':'/sys'}) && object.spec.template.spec.volumes.exists(v, v.name == 'root' && v.hostPath == {'path':'/'})",
      "object.spec.template.spec.containers[0].volumeMounts.size() == 3 && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'proc' && m.mountPath == '/host/proc' && m.readOnly == true) && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'sys' && m.mountPath == '/host/sys' && m.readOnly == true) && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'root' && m.mountPath == '/host/root' && m.mountPropagation == 'HostToContainer' && m.readOnly == true)",
    ]), var.pod_security_host_agent_images["node-exporter"])
    fs2-otel-node-agent = format(join(" && ", [
      "object.metadata.name == 'fs2-otel-node-agent'",
      "object.spec.template.spec.serviceAccountName == 'fs2-otel-node' && object.spec.template.spec.automountServiceAccountToken == true",
      "(!has(object.spec.template.spec.hostNetwork) || object.spec.template.spec.hostNetwork == false) && (!has(object.spec.template.spec.hostPID) || object.spec.template.spec.hostPID == false) && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false)",
      "object.spec.template.spec.securityContext == {'fsGroup':10001,'runAsGroup':10001,'runAsNonRoot':true,'runAsUser':10001,'seccompProfile':{'type':'RuntimeDefault'},'supplementalGroups':[0]}",
      "object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'opentelemetry-collector'",
      "object.spec.template.spec.containers[0].image == '%s' && object.spec.template.spec.containers[0].command == ['/otelcol-k8s'] && object.spec.template.spec.containers[0].args == ['--config=/conf/relay.yaml']",
      "object.spec.template.spec.containers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':true}",
      "object.spec.template.spec.containers[0].env.size() == 9 && object.spec.template.spec.containers[0].env.all(e, e.name in ['MY_POD_IP','OTEL_K8S_NODE_NAME','OTEL_K8S_NODE_IP','OTEL_K8S_NAMESPACE','OTEL_K8S_POD_NAME','OTEL_K8S_POD_IP','K8S_NODE_NAME','K8S_NODE_IP','GOMEMLIMIT'] && (!has(e.valueFrom) || !has(e.valueFrom.secretKeyRef))) && (!has(object.spec.template.spec.containers[0].envFrom) || object.spec.template.spec.containers[0].envFrom.size() == 0) && !has(object.spec.template.spec.containers[0].lifecycle)",
      "object.spec.template.spec.volumes.filter(v, !v.name.startsWith('kube-api-access-')).size() == 3 && object.spec.template.spec.volumes.filter(v, !v.name.startsWith('kube-api-access-')).all(v, v.name in ['opentelemetry-collector-configmap','varlogpods','varlibdockercontainers'])",
      local.pod_security_projected_api_token_expression,
      "object.spec.template.spec.volumes.exists(v, v.name == 'opentelemetry-collector-configmap' && v.configMap.name == '${local.otel_node_config_map_name}')",
      "object.spec.template.spec.volumes.exists(v, v.name == 'varlogpods' && v.hostPath == {'path':'/var/log/pods'}) && object.spec.template.spec.volumes.exists(v, v.name == 'varlibdockercontainers' && v.hostPath == {'path':'/var/lib/docker/containers'})",
      "object.spec.template.spec.containers[0].volumeMounts.filter(m, !m.name.startsWith('kube-api-access-')).size() == 3 && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'opentelemetry-collector-configmap' && m.mountPath == '/conf') && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'varlogpods' && m.mountPath == '/var/log/pods' && m.readOnly == true) && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'varlibdockercontainers' && m.mountPath == '/var/lib/docker/containers' && m.readOnly == true)",
    ]), var.pod_security_host_agent_images["otel-node"])
    fs2-dcgm-exporter = format(join(" && ", [
      "object.metadata.name == 'fs2-dcgm-exporter'",
      "object.spec.template.spec.serviceAccountName == 'fs2-dcgm-exporter' && object.spec.template.spec.automountServiceAccountToken == true",
      "(!has(object.spec.template.spec.hostNetwork) || object.spec.template.spec.hostNetwork == false) && object.spec.template.spec.hostPID == false && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false)",
      "!has(object.spec.template.spec.securityContext) || object.spec.template.spec.securityContext == {}",
      "object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'exporter'",
      "object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command) && object.spec.template.spec.containers[0].args == ['--collect-interval=5000']",
      "object.spec.template.spec.containers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'add':['SYS_ADMIN'],'drop':['ALL']},'runAsNonRoot':false,'runAsUser':0}",
      "object.spec.template.spec.containers[0].env.size() == 8 && object.spec.template.spec.containers[0].env.all(e, e.name in ['DCGM_EXPORTER_KUBERNETES','DCGM_EXPORTER_KUBERNETES_ENABLE_POD_LABELS','DCGM_EXPORTER_KUBERNETES_ENABLE_POD_UID','DCGM_EXPORTER_KUBERNETES_POD_LABEL_ALLOWLIST_REGEX','DCGM_EXPORTER_LISTEN','DCGM_EXPORTER_WEB_READ_TIMEOUT','DCGM_EXPORTER_WEB_WRITE_TIMEOUT','NODE_NAME'] && (!has(e.valueFrom) || !has(e.valueFrom.secretKeyRef))) && (!has(object.spec.template.spec.containers[0].envFrom) || object.spec.template.spec.containers[0].envFrom.size() == 0) && !has(object.spec.template.spec.containers[0].lifecycle)",
      "object.spec.template.spec.volumes.filter(v, !v.name.startsWith('kube-api-access-')).size() == 2 && object.spec.template.spec.volumes.filter(v, !v.name.startsWith('kube-api-access-')).all(v, v.name in ['pod-gpu-resources','exporter-metrics-volume'])",
      local.pod_security_projected_api_token_expression,
      "object.spec.template.spec.volumes.exists(v, v.name == 'pod-gpu-resources' && v.hostPath == {'path':'/var/lib/kubelet/pod-resources'}) && object.spec.template.spec.volumes.exists(v, v.name == 'exporter-metrics-volume' && v.configMap.name == '${local.dcgm_metrics_config_name}')",
      "object.spec.template.spec.containers[0].volumeMounts.filter(m, !m.name.startsWith('kube-api-access-')).size() == 2 && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'pod-gpu-resources' && m.mountPath == '/var/lib/kubelet/pod-resources' && m.readOnly == true) && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'exporter-metrics-volume' && m.mountPath == '/etc/dcgm-exporter/default-counters.csv' && m.subPath == 'default-counters.csv')",
    ]), var.pod_security_host_agent_images["dcgm-exporter"])
    fs2-serve-control-plane-gpu-observer = format(join(" && ", [
      "object.metadata.name == 'fs2-serve-control-plane-gpu-observer'",
      "object.spec.template.spec.serviceAccountName == 'fs2-serve-control-plane-gpu-observer' && object.spec.template.spec.automountServiceAccountToken == false",
      "(!has(object.spec.template.spec.hostNetwork) || object.spec.template.spec.hostNetwork == false) && (!has(object.spec.template.spec.hostPID) || object.spec.template.spec.hostPID == false) && (!has(object.spec.template.spec.hostIPC) || object.spec.template.spec.hostIPC == false)",
      "!has(object.spec.template.spec.securityContext) || object.spec.template.spec.securityContext == {}",
      "object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'observer'",
      "object.spec.template.spec.containers[0].image == '%s' && !has(object.spec.template.spec.containers[0].command) && object.spec.template.spec.containers[0].args == ['gpu-allocation-observer']",
      "object.spec.template.spec.containers[0].securityContext == {'runAsUser':0,'runAsGroup':0,'allowPrivilegeEscalation':false,'readOnlyRootFilesystem':true,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}}",
      "object.spec.template.spec.containers[0].env.size() == 7 && object.spec.template.spec.containers[0].env.all(e, e.name in ['FS2_GPU_ALLOCATION_OBSERVER_NODE_NAME','FS2_GPU_ALLOCATION_OBSERVER_NAMESPACES','FS2_GPU_ALLOCATION_OBSERVER_API_URL','FS2_GPU_ALLOCATION_OBSERVER_TOKEN_FILE','FS2_GPU_ALLOCATION_OBSERVER_CA_FILE','FS2_GPU_ALLOCATION_OBSERVER_CHECKPOINT_FILE','FS2_GPU_ALLOCATION_OBSERVER_POLL_SECONDS'] && (!has(e.valueFrom) || !has(e.valueFrom.secretKeyRef))) && object.spec.template.spec.containers[0].env.exists(e, e.name == 'FS2_GPU_ALLOCATION_OBSERVER_API_URL' && e.value == 'https://kubernetes.default.svc') && object.spec.template.spec.containers[0].env.exists(e, e.name == 'FS2_GPU_ALLOCATION_OBSERVER_TOKEN_FILE' && e.value == '/var/run/secrets/fs2-serve/gpu-observer/token') && object.spec.template.spec.containers[0].env.exists(e, e.name == 'FS2_GPU_ALLOCATION_OBSERVER_CA_FILE' && e.value == '/var/run/secrets/fs2-serve/gpu-observer/ca.crt') && object.spec.template.spec.containers[0].env.exists(e, e.name == 'FS2_GPU_ALLOCATION_OBSERVER_CHECKPOINT_FILE' && e.value == '/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint') && object.spec.template.spec.containers[0].env.exists(e, e.name == 'FS2_GPU_ALLOCATION_OBSERVER_POLL_SECONDS' && e.value == '1') && (!has(object.spec.template.spec.containers[0].envFrom) || object.spec.template.spec.containers[0].envFrom.size() == 0) && !has(object.spec.template.spec.containers[0].lifecycle)",
      "object.spec.template.spec.volumes.size() == 2 && object.spec.template.spec.volumes.exists(v, v.name == 'kubelet-device-plugins' && v.hostPath.path == '/var/lib/kubelet/device-plugins' && v.hostPath.type == 'Directory') && object.spec.template.spec.volumes.exists(v, v.name == 'kubernetes-token' && v.projected == {'defaultMode':256,'sources':[{'serviceAccountToken':{'expirationSeconds':600,'path':'token'}},{'configMap':{'name':'kube-root-ca.crt','items':[{'key':'ca.crt','path':'ca.crt'}]}}]})",
      "object.spec.template.spec.containers[0].volumeMounts.size() == 2 && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'kubelet-device-plugins' && m.mountPath == '/var/lib/kubelet/device-plugins' && m.readOnly == true) && object.spec.template.spec.containers[0].volumeMounts.exists(m, m.name == 'kubernetes-token' && m.mountPath == '/var/run/secrets/fs2-serve/gpu-observer' && m.readOnly == true)",
    ]), var.pod_security_host_agent_images["gpu-observer"])
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

# Privileged host agents may consume only content-addressed immutable
# configuration. Admission binds the complete data map on CREATE and refuses
# later UPDATE/DELETE operations, including metadata-only rewrites.
resource "kubernetes_manifest" "node_observability_config_policy" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-node-observability-configs"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["configmaps"]
        }]
      }
      matchConditions = [{
        name = "reviewed-host-agent-config"
        expression = format(
          "request.operation == 'CREATE' ? object.metadata.name in %s : oldObject.metadata.name in %s",
          jsonencode([
            local.otel_node_config_map_name,
            local.dcgm_metrics_config_name,
            local.dcgm_cold_config_map_name,
          ]),
          jsonencode([
            local.otel_node_config_map_name,
            local.dcgm_metrics_config_name,
            local.dcgm_cold_config_map_name,
          ]),
        )
      }]
      validations = [{
        expression = format(
          "request.operation == 'CREATE' && object.immutable == true && ((object.metadata.name == '%s' && object.data == %s) || (object.metadata.name == '%s' && object.data == %s) || (object.metadata.name == '%s' && object.data == %s))",
          local.otel_node_config_map_name,
          jsonencode({ relay = local.otel_node_relay }),
          local.dcgm_metrics_config_name,
          jsonencode({ metrics = local.dcgm_metrics }),
          local.dcgm_cold_config_map_name,
          jsonencode({ "config.yaml" = local.dcgm_cold_config }),
        )
        message = "Host-agent configuration is immutable and must equal one reviewed content-addressed data map."
      }]
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "node_observability_config_binding" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-node-observability-configs"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-node-observability-configs"
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
    kubernetes_manifest.node_observability_config_policy,
    kubernetes_namespace_v1.platform,
  ]
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
      "podtemplates",
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
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["roles", "rolebindings"]
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

# Installed during the signed baseline bootstrap and inactive in ordinary
# states. The ledger CAS activates this fence atomically at
# enforcement-quiesced; it remains active through an unacknowledged enforce
# transition. No Pod-producing object can race into a namespace between the
# final signed read and the pinned PSA label apply.
resource "kubernetes_manifest" "pod_security_enforcement_fence_policy" {
  count = local.pod_security_receipt_required ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-pod-security-enforcement-fence"
      labels = local.common_labels
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
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods", "podtemplates", "replicationcontrollers"]
          },
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["daemonsets", "deployments", "replicasets", "statefulsets"]
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["cronjobs", "jobs"]
          },
          {
            apiGroups   = ["jobset.x-k8s.io"]
            apiVersions = ["v1alpha2"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobsets"]
          },
          {
            apiGroups   = ["inference.fs2.nebius.ai"]
            apiVersions = ["v1alpha1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["modeldeployments"]
          },
        ]
      }
      validations = [{
        expression = "!(params.data.state == 'enforcement-quiesced' || (params.data.authorization_phase == 'enforce' && params.data.authorization_downstream_acknowledged != 'true'))"
        message    = "Pod-producing writes are fenced while baseline enforcement is quiesced or awaiting both stages' applied-state acknowledgement."
      }]
    }
  }

  depends_on = [module.pod_security_rollout_gate]
}

resource "kubernetes_manifest" "pod_security_enforcement_fence_binding" {
  count = local.pod_security_receipt_required ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-pod-security-enforcement-fence"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-pod-security-enforcement-fence"
      validationActions = ["Deny"]
      paramRef = {
        name                    = "fs2-pod-security-rollout-ledger"
        namespace               = "fs2-system"
        parameterNotFoundAction = "Deny"
      }
      matchResources = {
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values = concat([
              "fs2-data",
              "fs2-models",
              "fs2-observability",
              "fs2-reference-data",
              "fs2-system",
            ], local.pod_security_scientific_namespaces)
          }]
        }
      }
    }
  }

  depends_on = [kubernetes_manifest.pod_security_enforcement_fence_policy]
}

# Once the exception agents and finite profiles are ready, this admission
# fence freezes every exact legacy cleanup identity and rejects new consumers
# of a legacy ServiceAccount. The cleanup tool can therefore remove DS/NP
# first, re-read SA consumers, and remove SAs without a create/update race.
resource "kubernetes_manifest" "pod_security_legacy_cleanup_fence_policy" {
  count = local.pod_security_receipt_required ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-pod-security-legacy-cleanup-fence"
      labels = local.common_labels
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
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods", "podtemplates", "replicationcontrollers", "serviceaccounts"]
          },
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["daemonsets", "deployments", "replicasets", "statefulsets"]
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["cronjobs", "jobs"]
          },
          {
            apiGroups   = ["jobset.x-k8s.io"]
            apiVersions = ["v1alpha2"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobsets"]
          },
          {
            apiGroups   = ["inference.fs2.nebius.ai"]
            apiVersions = ["v1alpha1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["modeldeployments"]
          },
          {
            apiGroups   = ["networking.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["networkpolicies"]
          },
        ]
      }
      variables = [{
        name       = "cleanupActive"
        expression = "params.data.state in ['reference-data-ready','enforcement-quiesced','baseline-enforced']"
      }]
      validations = [
        {
          expression = format(
            "!variables.cleanupActive || !((object.kind == 'NetworkPolicy' && object.metadata.name in %s) || (object.kind == 'ServiceAccount' && object.metadata.name in %s) || (object.kind == 'DaemonSet' && object.metadata.name in %s))",
            jsonencode(local.pod_security_legacy_cleanup_names.networkpolicies),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.daemonsets),
          )
          message = "Exact legacy cleanup identities are frozen while their UID/resourceVersion-bound transition is active."
        },
        {
          expression = format(
            "!variables.cleanupActive || (object.kind == 'Pod' ? (!has(object.spec.serviceAccountName) || !(object.spec.serviceAccountName in %s)) : object.kind == 'PodTemplate' ? (!has(object.template.spec.serviceAccountName) || !(object.template.spec.serviceAccountName in %s)) : object.kind in ['Deployment','StatefulSet','DaemonSet','ReplicaSet','ReplicationController','Job'] ? (!has(object.spec.template.spec.serviceAccountName) || !(object.spec.template.spec.serviceAccountName in %s)) : object.kind == 'CronJob' ? (!has(object.spec.jobTemplate.spec.template.spec.serviceAccountName) || !(object.spec.jobTemplate.spec.template.spec.serviceAccountName in %s)) : object.kind == 'JobSet' ? object.spec.replicatedJobs.all(r, !has(r.template.spec.template.spec.serviceAccountName) || !(r.template.spec.template.spec.serviceAccountName in %s)) : object.kind == 'ModelDeployment' ? (!has(object.spec.template) || !has(object.spec.template.spec) || !has(object.spec.template.spec.serviceAccountName) || !(object.spec.template.spec.serviceAccountName in %s)) : true)",
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
          )
          message = "No Pod or supported controller may acquire a legacy ServiceAccount while cleanup is fenced."
        },
      ]
    }
  }

  depends_on = [module.pod_security_rollout_gate]
}

resource "kubernetes_manifest" "pod_security_legacy_cleanup_fence_binding" {
  count = local.pod_security_receipt_required ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-pod-security-legacy-cleanup-fence"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-pod-security-legacy-cleanup-fence"
      validationActions = ["Deny"]
      paramRef = {
        name                    = "fs2-pod-security-rollout-ledger"
        namespace               = "fs2-system"
        parameterNotFoundAction = "Deny"
      }
      matchResources = {
        namespaceSelector = {
          matchLabels = {
            "kubernetes.io/metadata.name" = "fs2-models"
          }
        }
      }
    }
  }

  depends_on = [kubernetes_manifest.pod_security_legacy_cleanup_fence_policy]
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
          expression = "request.operation == 'DELETE' || (object.metadata.uid == oldObject.metadata.uid && object.metadata.name == oldObject.metadata.name && object.metadata.namespace == oldObject.metadata.namespace)"
          message    = "The rollout ledger identity is immutable."
        },
        {
          expression = "request.operation == 'DELETE' || (object.data.size() == 15 && ['schema','context_sha256','authority_key_id','authority_signer_identity','authority_public_key_sha256','sequence','state','last_bundle_sha256','last_receipt_id','last_nonce','authorization_phase','authorization_bundle_sha256','authorization_nonce','authorization_owner_acknowledged','authorization_downstream_acknowledged'].all(k, k in object.data))"
          message    = "The rollout ledger must contain exactly the canonical admission-readable fields."
        },
        {
          expression = "request.operation == 'DELETE' || (object.data.schema == oldObject.data.schema && object.data.context_sha256 == oldObject.data.context_sha256 && object.data.authority_key_id == oldObject.data.authority_key_id && object.data.authority_signer_identity == oldObject.data.authority_signer_identity && object.data.authority_public_key_sha256 == oldObject.data.authority_public_key_sha256)"
          message    = "The rollout ledger context and signing authority are immutable."
        },
        {
          expression = "request.operation == 'DELETE' || ((int(object.data.sequence) == int(oldObject.data.sequence) + 1 && (oldObject.data.sequence == '0' || (oldObject.data.authorization_owner_acknowledged == 'true' && oldObject.data.authorization_downstream_acknowledged == 'true')) && ((oldObject.data.state == 'unmanaged' && object.data.state == 'baseline-captured') || (oldObject.data.state == 'baseline-captured' && object.data.state == 'exception-ready') || (oldObject.data.state == 'exception-ready' && object.data.state == 'reference-data-ready') || (oldObject.data.state == 'reference-data-ready' && object.data.state == 'enforcement-quiesced') || (oldObject.data.state == 'enforcement-quiesced' && object.data.state == 'baseline-enforced') || (oldObject.data.state == 'baseline-enforced' && object.data.state == 'enforcement-removed') || (oldObject.data.state == 'enforcement-removed' && object.data.state == 'host-agents-restored') || (oldObject.data.state == 'host-agents-restored' && object.data.state == 'rolled-back')) && object.data.last_bundle_sha256.matches('^[a-f0-9]{64}$') && object.data.last_receipt_id != '' && object.data.last_receipt_id != oldObject.data.last_receipt_id && object.data.last_nonce != '' && object.data.last_nonce != oldObject.data.last_nonce && object.data.authorization_phase in ['bootstrap-baseline','migrate-reference-data','cleanup-legacy-resources','quiesce-enforcement','enforce','rollback-remove-enforcement','rollback-restore-host-agents','rollback-remove-exception'] && object.data.authorization_bundle_sha256 == object.data.last_bundle_sha256 && object.data.authorization_nonce == object.data.last_nonce && object.data.authorization_owner_acknowledged == 'false' && object.data.authorization_downstream_acknowledged == 'false') || (object.data.sequence == oldObject.data.sequence && object.data.state == oldObject.data.state && object.data.last_bundle_sha256 == oldObject.data.last_bundle_sha256 && object.data.last_receipt_id == oldObject.data.last_receipt_id && object.data.last_nonce == oldObject.data.last_nonce && object.data.authorization_phase == oldObject.data.authorization_phase && object.data.authorization_bundle_sha256 == oldObject.data.authorization_bundle_sha256 && object.data.authorization_nonce == oldObject.data.authorization_nonce && ((oldObject.data.authorization_owner_acknowledged == 'false' && object.data.authorization_owner_acknowledged == 'true' && object.data.authorization_downstream_acknowledged == oldObject.data.authorization_downstream_acknowledged) || (object.data.authorization_owner_acknowledged == oldObject.data.authorization_owner_acknowledged && oldObject.data.authorization_downstream_acknowledged == 'false' && object.data.authorization_downstream_acknowledged == 'true'))))"
          message    = "The rollout ledger may only advance one reviewed edge after both prior acknowledgements, or monotonically acknowledge the current exact authorization."
        },
        {
          expression = "request.operation == 'DELETE' || ((object.data.state == 'baseline-captured' && object.data.authorization_phase == 'bootstrap-baseline') || (object.data.state == 'exception-ready' && object.data.authorization_phase == 'migrate-reference-data') || (object.data.state == 'reference-data-ready' && object.data.authorization_phase == 'cleanup-legacy-resources') || (object.data.state == 'enforcement-quiesced' && object.data.authorization_phase == 'quiesce-enforcement') || (object.data.state == 'baseline-enforced' && object.data.authorization_phase == 'enforce') || (object.data.state == 'enforcement-removed' && object.data.authorization_phase == 'rollback-remove-enforcement') || (object.data.state == 'host-agents-restored' && object.data.authorization_phase == 'rollback-restore-host-agents') || (object.data.state == 'rolled-back' && object.data.authorization_phase == 'rollback-remove-exception'))"
          message    = "The rollout ledger state must authorize only its matching next deployment phase."
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
