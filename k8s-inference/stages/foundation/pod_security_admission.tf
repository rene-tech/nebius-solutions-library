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
  pod_security_rollout_manager_username   = "system:serviceaccount:fs2-system:fs2-pod-security-rollout-manager"
  pod_security_rollout_custodian_username = "system:serviceaccount:fs2-system:fs2-pod-security-rollout-custodian"
  pod_security_rollout_custodian_group    = "fs2-pod-security-receipt-custodians"
  pod_security_rollout_custodian_external_username = coalesce(
    var.pod_security_rollout_receipt.custody_username,
    "pod-security-receipt-operator-not-configured",
  )
  pod_security_custody_owner_username = coalesce(
    var.pod_security_rollout_receipt.custody_owner_username,
    "pod-security-custody-owner-not-configured",
  )
  pod_security_custody_owner_group = var.pod_security_rollout_receipt.custody_owner_group
  pod_security_platform_username = coalesce(
    var.pod_security_rollout_receipt.platform_username,
    "pod-security-platform-not-configured",
  )
  pod_security_platform_group         = var.pod_security_rollout_receipt.platform_group
  pod_security_rollout_token_audience = "https://kubernetes.default.svc"
  pod_security_custody_epoch_sha256   = var.pod_security_rollout_receipt.custody_epoch_sha256
  pod_security_token_anchor_name      = "fs2-pod-security-token-anchor-v3-${local.pod_security_custody_epoch_sha256}"
  pod_security_custody_protected_names = {
    serviceaccounts = [
      "fs2-pod-security-metadata-reader",
      "fs2-pod-security-rollout-custodian",
      "fs2-pod-security-rollout-manager",
    ]
    configmaps = ["fs2-pod-security-rollout-ledger"]
    roles = [
      "fs2-pod-security-rollout-ledger",
      "fs2-pod-security-metadata-reader-token-request",
      "fs2-pod-security-rollout-token-request",
      "fs2-pod-security-secret-metadata-reader",
      "fs2-pod-security-token-anchor-metadata-reader",
    ]
    rolebindings = [
      "fs2-pod-security-rollout-custodian-ledger",
      "fs2-pod-security-rollout-ledger",
      "fs2-pod-security-metadata-reader-token-request",
      "fs2-pod-security-rollout-token-request",
      "fs2-pod-security-secret-metadata-reader",
      "fs2-pod-security-token-anchor-metadata-reader",
    ]
    clusterroles = [
      "fs2-pod-security-external-custody-audit",
      "fs2-pod-security-rollout-reader",
    ]
    clusterrolebindings = [
      "fs2-pod-security-external-custody-audit",
      "fs2-pod-security-rollout-custodian-reader",
      "fs2-pod-security-rollout-reader",
    ]
    admission = [
      "fs2-node-observability-configs",
      "fs2-node-observability-daemonsets",
      "fs2-node-observability-pods",
      "fs2-pod-security-custody-boundary",
      "fs2-pod-security-enforcement-fence",
      "fs2-pod-security-legacy-cleanup-fence",
      "fs2-pod-security-rollout-ledger",
      "fs2-pod-security-rollout-token-request",
      "fs2-snapshot-exact-profile",
    ]
  }
  pod_security_external_ack_prefix = "fs2-sai07-custody-ack-v3-"
  pod_security_custody_additive_object_match = "object.metadata.namespace == 'fs2-system' && ((object.kind == 'Secret' && (object.metadata.name == 'fs2-pod-security-token-anchor' || object.metadata.name.startsWith('fs2-pod-security-token-anchor-v3-'))) || (object.kind == 'ConfigMap' && object.metadata.name.startsWith('${local.pod_security_external_ack_prefix}')))"
  pod_security_custody_additive_old_object_match = "oldObject.metadata.namespace == 'fs2-system' && ((oldObject.kind == 'Secret' && (oldObject.metadata.name == 'fs2-pod-security-token-anchor' || oldObject.metadata.name.startsWith('fs2-pod-security-token-anchor-v3-'))) || (oldObject.kind == 'ConfigMap' && oldObject.metadata.name.startsWith('${local.pod_security_external_ack_prefix}')))"
  pod_security_rollout_persistent_volumes = sort(distinct(concat(
    [
      "fs2-sai07-ref-bioir-boltz2",
      "fs2-sai07-ref-bioir-coverage",
      "fs2-sai07-ref-bioir-openfold",
      "fs2-sai07-ref-bioir-protenix",
      "fs2-sai07-ref-bioir-snapshot",
      "fs2-sai07-ref-snapshot-operations",
    ],
    local.pod_security_receipt_required ? [
      jsondecode(var.pod_security_successor_storage_json).reference_source.persistent_volume_name,
      jsondecode(var.pod_security_successor_storage_json).checkpoint_source.persistent_volume_name,
    ] : [],
  )))
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

# RETAINED PLATFORM-STATE CUSTODY DECLARATIONS.
#
# These addresses remain active solely so the platform state cannot interpret
# their absence as permission to destroy or forget live admission/RBAC objects.
# The external v3 executor owns zero fields on them and creates only its
# generation acknowledgement plus the metadata-only empty token anchor. This
# source does not claim that a VAP protects itself; the blocked external trust
# contract and exhaustive owner/platform authority audits are the preventive
# boundary. Every declaration is prevent_destroy and rollout-gated.

resource "kubernetes_manifest" "pod_security_custody_boundary_policy" {
  provider = kubernetes.pod_security_custody

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-pod-security-custody-boundary"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["configmaps", "secrets", "serviceaccounts"]
          },
          {
            apiGroups   = ["rbac.authorization.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
          },
          {
            apiGroups   = ["admissionregistration.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
          },
        ]
      }
      matchConditions = [{
        name = "exact-custody-object"
        expression = format(
          "request.userInfo.username == '%s' || (request.operation == 'DELETE' ? ((%s) || (((oldObject.kind == 'ServiceAccount' && oldObject.metadata.name in %s) || (oldObject.kind == 'ConfigMap' && oldObject.metadata.name in %s) || (oldObject.kind == 'Role' && oldObject.metadata.name in %s) || (oldObject.kind == 'RoleBinding' && oldObject.metadata.name in %s)) && oldObject.metadata.namespace == 'fs2-system' || (oldObject.kind == 'ClusterRole' && oldObject.metadata.name in %s) || (oldObject.kind == 'ClusterRoleBinding' && oldObject.metadata.name in %s) || (oldObject.kind in ['ValidatingAdmissionPolicy','ValidatingAdmissionPolicyBinding'] && oldObject.metadata.name in %s))) : ((%s) || ((((object.kind == 'ServiceAccount' && object.metadata.name in %s) || (object.kind == 'ConfigMap' && object.metadata.name in %s) || (object.kind == 'Role' && object.metadata.name in %s) || (object.kind == 'RoleBinding' && object.metadata.name in %s)) && object.metadata.namespace == 'fs2-system') || (object.kind == 'ClusterRole' && object.metadata.name in %s) || (object.kind == 'ClusterRoleBinding' && object.metadata.name in %s) || (object.kind in ['ValidatingAdmissionPolicy','ValidatingAdmissionPolicyBinding'] && object.metadata.name in %s))))",
          local.pod_security_custody_owner_username,
          local.pod_security_custody_additive_old_object_match,
          jsonencode(local.pod_security_custody_protected_names.serviceaccounts),
          jsonencode(local.pod_security_custody_protected_names.configmaps),
          jsonencode(local.pod_security_custody_protected_names.roles),
          jsonencode(local.pod_security_custody_protected_names.rolebindings),
          jsonencode(local.pod_security_custody_protected_names.clusterroles),
          jsonencode(local.pod_security_custody_protected_names.clusterrolebindings),
          jsonencode(local.pod_security_custody_protected_names.admission),
          local.pod_security_custody_additive_object_match,
          jsonencode(local.pod_security_custody_protected_names.serviceaccounts),
          jsonencode(local.pod_security_custody_protected_names.configmaps),
          jsonencode(local.pod_security_custody_protected_names.roles),
          jsonencode(local.pod_security_custody_protected_names.rolebindings),
          jsonencode(local.pod_security_custody_protected_names.clusterroles),
          jsonencode(local.pod_security_custody_protected_names.clusterrolebindings),
          jsonencode(local.pod_security_custody_protected_names.admission),
        )
      }]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "Custody objects are retained and may not be deleted."
        },
        {
          expression = "request.userInfo.username != '${local.pod_security_custody_owner_username}' || (${local.pod_security_custody_additive_object_match})"
          message    = "The external execution identity may create only the exact token anchor or a generation-addressed acknowledgement."
        },
        {
          expression = "object.kind == 'Secret' && (object.metadata.name == 'fs2-pod-security-token-anchor' || object.metadata.name.startsWith('fs2-pod-security-token-anchor-v3-')) ? (request.operation == 'CREATE' && object.metadata.name == '${local.pod_security_token_anchor_name}' && object.metadata.namespace == 'fs2-system' && object.immutable == true && object.type == 'Opaque' && object.data == {} && !has(object.stringData) && object.metadata.labels == {'security.fs2.nebius.ai/custody-owner':'external','security.fs2.nebius.ai/role':'token-anchor'} && object.metadata.annotations == {'security.fs2.nebius.ai/custody-epoch-sha256':'${local.pod_security_custody_epoch_sha256}'} && (!has(object.metadata.ownerReferences) || object.metadata.ownerReferences.size() == 0) && (!has(object.metadata.finalizers) || object.metadata.finalizers.size() == 0)) : object.kind == 'ConfigMap' && object.metadata.name.startsWith('${local.pod_security_external_ack_prefix}') ? (request.operation == 'CREATE' && object.metadata.namespace == 'fs2-system' && object.immutable == true && object.data.size() == 1 && object.data['execution.json'].size() > 0 && object.data['execution.json'].size() <= 262144 && !has(object.binaryData) && object.metadata.labels == {'app.kubernetes.io/managed-by':'fs2-sai07-external-custody','security.fs2.nebius.ai/role':'external-execution-acknowledgement'} && object.metadata.annotations.size() == 2 && object.metadata.annotations['security.fs2.nebius.ai/contract-sha256'].matches('^[a-f0-9]{64}$') && object.metadata.annotations['security.fs2.nebius.ai/execution-generation'].matches('^[a-f0-9]{64}$') && (!has(object.metadata.ownerReferences) || object.metadata.ownerReferences.size() == 0) && (!has(object.metadata.finalizers) || object.metadata.finalizers.size() == 0)) : true"
          message    = "The additive token anchor and acknowledgement must match their exact immutable empty/content-bound profiles and can never be updated."
        },
        {
          expression = "(${local.pod_security_custody_additive_object_match}) ? (request.userInfo.username == '${local.pod_security_custody_owner_username}' && !request.userInfo.username.startsWith('system:')) : (object.kind == 'ConfigMap' && object.metadata.namespace == 'fs2-system' && object.metadata.name == 'fs2-pod-security-rollout-ledger' && request.userInfo.username in ['${local.pod_security_rollout_manager_username}','${local.pod_security_rollout_custodian_username}'])"
          message    = "Only the separately administered owner may create additive custody objects; only the two short-lived rollout identities may advance the exact ledger."
        },
        {
          expression = "!(${local.pod_security_custody_additive_object_match}) || (request.userInfo.groups.exists(g, g == '${local.pod_security_custody_owner_group}') && !request.userInfo.groups.exists(g, g in ['system:masters','${local.pod_security_platform_group}','${local.pod_security_rollout_custodian_group}']))"
          message    = "Custody writes require the dedicated owner group and exclude platform, receipt, and system:masters groups."
        },
        {
          expression = "has(request.userInfo.extra) && 'authentication.kubernetes.io/credential-id' in request.userInfo.extra && request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1 && request.userInfo.extra['authentication.kubernetes.io/credential-id'][0].startsWith('JTI=')"
          message    = "Custody writes require a directly authenticated, non-impersonated credential."
        },
      ]
    }
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        var.pod_security_rollout_receipt.custody_owner_kubeconfig_path == null &&
        var.pod_security_rollout_receipt.custody_kubeconfig_path == null &&
        var.pod_security_rollout_receipt.custody_owner_context == null &&
        var.pod_security_rollout_receipt.custody_context == null &&
        can(regex("^[a-f0-9]{64}$", local.pod_security_custody_epoch_sha256)) &&
        local.pod_security_custody_epoch_sha256 != strrep("0", 64)
      )
      error_message = "Platform Terraform must not receive external custody credentials and must bind the exact active nonzero custody epoch."
    }
  }

  depends_on = [
    kubernetes_role_binding_v1.pod_security_metadata_reader_token_request,
    kubernetes_role_binding_v1.pod_security_secret_metadata_reader,
    kubernetes_role_binding_v1.pod_security_token_anchor_metadata_reader,
  ]
}

resource "kubernetes_manifest" "pod_security_custody_boundary_binding" {
  provider = kubernetes.pod_security_custody

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-pod-security-custody-boundary"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-pod-security-custody-boundary"
      validationActions = ["Deny"]
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.pod_security_custody_boundary_policy]
}

# Privileged host agents may consume only content-addressed immutable
# configuration. Admission binds the complete data map on CREATE and refuses
# later UPDATE/DELETE operations, including metadata-only rewrites.
resource "kubernetes_manifest" "node_observability_config_policy" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    terraform_data.cluster_contract,
  ]
}

resource "kubernetes_manifest" "node_observability_config_binding" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    terraform_data.cluster_contract,
  ]
}

resource "kubernetes_manifest" "node_observability_pod_binding" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    terraform_data.cluster_contract,
  ]
}

resource "kubernetes_manifest" "node_observability_daemonset_binding" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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

# Retain the predecessor manager object for non-destructive state continuity.
# Its predecessor bindings also remain unchanged, but fail-closed ledger
# admission rejects that username. Receipt consumption uses the separately
# authenticated rollout-custodian identity below.
resource "kubernetes_service_account_v1" "pod_security_rollout_manager" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }

  metadata {
    name      = "fs2-pod-security-rollout-manager"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  automount_service_account_token = false

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    kubernetes_namespace_v1.platform,
  ]
}

# A distinct tokenless identity owns receipt consumption. New, separately
# named bindings avoid replacing the retained predecessor RBAC objects.
resource "kubernetes_service_account_v1" "pod_security_rollout_custodian" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
  metadata {
    name      = "fs2-pod-security-rollout-custodian"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  automount_service_account_token = false

  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    kubernetes_namespace_v1.platform,
  ]
}

resource "kubernetes_cluster_role_v1" "pod_security_rollout_reader" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
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
    api_groups     = [""]
    resources      = ["persistentvolumes"]
    resource_names = local.pod_security_rollout_persistent_volumes
    verbs          = ["get"]
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
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterroles"]
    resource_names = [
      "fs2-pod-security-external-custody-audit",
      "fs2-pod-security-rollout-reader",
    ]
    verbs = ["get"]
  }
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterrolebindings"]
    resource_names = [
      "fs2-pod-security-external-custody-audit",
      "fs2-pod-security-rollout-custodian-reader",
      "fs2-pod-security-rollout-reader",
    ]
    verbs = ["get"]
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
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
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

resource "kubernetes_cluster_role_binding_v1" "pod_security_rollout_custodian_reader" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
  metadata {
    name   = "fs2-pod-security-rollout-custodian-reader"
    labels = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.pod_security_rollout_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.pod_security_rollout_custodian.metadata[0].name
    namespace = kubernetes_service_account_v1.pod_security_rollout_custodian.metadata[0].namespace
  }
}

resource "kubernetes_role_v1" "pod_security_rollout_ledger" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
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
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
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

resource "kubernetes_role_binding_v1" "pod_security_rollout_custodian_ledger" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
  metadata {
    name      = "fs2-pod-security-rollout-custodian-ledger"
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
    name      = kubernetes_service_account_v1.pod_security_rollout_custodian.metadata[0].name
    namespace = kubernetes_service_account_v1.pod_security_rollout_custodian.metadata[0].namespace
  }
}

# The external receipt-custodian OIDC group may mint only a short-lived token
# for the exact rollout custodian service account.  It receives no impersonate
# verb, no direct ledger permission, and no reusable token Secret.
resource "kubernetes_cluster_role_v1" "pod_security_external_custody_audit" {
  provider = kubernetes.pod_security_custody

  metadata {
    name   = "fs2-pod-security-external-custody-audit"
    labels = local.common_labels
  }

  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    verbs      = ["get", "list"]
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]
}

resource "kubernetes_cluster_role_binding_v1" "pod_security_external_custody_audit" {
  provider = kubernetes.pod_security_custody

  metadata {
    name   = "fs2-pod-security-external-custody-audit"
    labels = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.pod_security_external_custody_audit.metadata[0].name
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = local.pod_security_rollout_custodian_group
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_role_v1" "pod_security_rollout_token_request" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
  metadata {
    name      = "fs2-pod-security-rollout-token-request"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts/token"]
    resource_names = [kubernetes_service_account_v1.pod_security_rollout_custodian.metadata[0].name]
    verbs          = ["create"]
  }
}

resource "kubernetes_role_binding_v1" "pod_security_rollout_token_request" {
  provider   = kubernetes.pod_security_custody
  depends_on = [kubernetes_manifest.pod_security_custody_boundary_binding]

  lifecycle {
    prevent_destroy = true
  }
  metadata {
    name      = "fs2-pod-security-rollout-token-request"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.pod_security_rollout_token_request.metadata[0].name
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = local.pod_security_rollout_custodian_group
  }
}

resource "kubernetes_manifest" "pod_security_rollout_token_policy" {
  provider = kubernetes.pod_security_custody
  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    kubernetes_service_account_v1.pod_security_metadata_reader,
    kubernetes_service_account_v1.pod_security_rollout_custodian,
  ]

  lifecycle {
    prevent_destroy = true
  }
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-pod-security-rollout-token-request"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE"]
          resources   = ["serviceaccounts/token"]
        }]
      }
      matchConditions = [{
        name       = "exact-bounded-reader-token"
        expression = "request.name in ['fs2-pod-security-metadata-reader','fs2-pod-security-rollout-custodian']"
      }]
      validations = [
        {
          expression = "request.userInfo.username == '${local.pod_security_rollout_custodian_external_username}'"
          message    = "Only the exact current external receipt operator may request a bounded reader token."
        },
        {
          expression = "request.userInfo.groups.exists(group, group == '${local.pod_security_rollout_custodian_group}')"
          message    = "Only the external receipt-custodian group may request a rollout token."
        },
        {
          expression = "!request.userInfo.username.startsWith('system:') && has(request.userInfo.extra) && 'authentication.kubernetes.io/credential-id' in request.userInfo.extra && request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1 && request.userInfo.extra['authentication.kubernetes.io/credential-id'][0].startsWith('JTI=')"
          message    = "Rollout TokenRequests require a directly authenticated external OIDC identity; service-account and impersonated identities are denied."
        },
        {
          expression = "object.spec.audiences == ['${local.pod_security_rollout_token_audience}'] && object.spec.expirationSeconds > 0 && object.spec.expirationSeconds <= 600"
          message    = "The rollout token must be API-audience bound and expire within ten minutes."
        },
        {
          expression = "has(object.spec.boundObjectRef) && object.spec.boundObjectRef.apiVersion == 'v1' && object.spec.boundObjectRef.kind == 'Secret' && object.spec.boundObjectRef.name == '${local.pod_security_token_anchor_name}' && object.spec.boundObjectRef.uid != ''"
          message    = "Every bounded reader token must be bound to the exact immutable custody-epoch anchor Secret."
        },
      ]
    }
  }

}

resource "kubernetes_manifest" "pod_security_rollout_token_binding" {
  provider = kubernetes.pod_security_custody
  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    kubernetes_manifest.pod_security_rollout_token_policy,
  ]

  lifecycle {
    prevent_destroy = true
  }
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-pod-security-rollout-token-request"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-pod-security-rollout-token-request"
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

}

# Installed during the signed baseline bootstrap and inactive in ordinary
# states. The ledger CAS activates this fence atomically at
# enforcement-quiesced; it remains active through an unacknowledged enforce
# transition. No Pod-producing object can race into a namespace between the
# final signed read and the pinned PSA label apply.
resource "kubernetes_manifest" "pod_security_enforcement_fence_policy" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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
# fence freezes every exact retained legacy identity, rejects new consumers of
# a legacy ServiceAccount and rejects Pods owned by a retained DaemonSet. The
# read-only verifier accepts only inert deny-only/tokenless/zero-Pod objects;
# no delete or rewrite is part of this closure.
resource "kubernetes_manifest" "pod_security_legacy_cleanup_fence_policy" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["pods", "podtemplates", "replicationcontrollers", "secrets", "serviceaccounts"]
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
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["DELETE"]
            resources   = ["serviceaccounts"]
          },
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["DELETE"]
            resources   = ["daemonsets"]
          },
          {
            apiGroups   = ["networking.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["DELETE"]
            resources   = ["networkpolicies"]
          },
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE"]
            resources   = ["serviceaccounts/token"]
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
            "!variables.cleanupActive || (request.operation == 'DELETE' ? !((oldObject.kind == 'NetworkPolicy' && oldObject.metadata.name in %s) || (oldObject.kind == 'ServiceAccount' && oldObject.metadata.name in %s) || (oldObject.kind == 'DaemonSet' && oldObject.metadata.name in %s)) : object.kind == 'NetworkPolicy' && object.metadata.name in %s ? (request.operation == 'UPDATE' && request.userInfo.username == '%s' && request.userInfo.groups.exists(g, g == '%s') && !request.userInfo.groups.exists(g, g in ['system:masters','%s','%s']) && object.metadata.labels['security.fs2.nebius.ai/retained-quarantine'] == 'true' && object.metadata.annotations['security.fs2.nebius.ai/baseline-uid'] == oldObject.metadata.uid && object.metadata.annotations['security.fs2.nebius.ai/baseline-object-sha256'].matches('^[a-f0-9]{64}$') && object.spec == {'podSelector':{'matchLabels':{'security.fs2.nebius.ai/retained-quarantine':'true'}},'policyTypes':['Ingress','Egress'],'ingress':[],'egress':[]}) : object.kind == 'ServiceAccount' && object.metadata.name in %s ? (request.operation == 'UPDATE' && request.userInfo.username == '%s' && request.userInfo.groups.exists(g, g == '%s') && !request.userInfo.groups.exists(g, g in ['system:masters','%s','%s']) && object.metadata.labels['security.fs2.nebius.ai/retained-quarantine'] == 'true' && object.metadata.annotations['security.fs2.nebius.ai/baseline-uid'] == oldObject.metadata.uid && object.metadata.annotations['security.fs2.nebius.ai/baseline-object-sha256'].matches('^[a-f0-9]{64}$') && object.automountServiceAccountToken == false && (!has(object.secrets) || object.secrets.size() == 0) && (!has(object.imagePullSecrets) || object.imagePullSecrets.size() == 0)) : object.kind == 'DaemonSet' && object.metadata.name in %s ? (request.operation == 'UPDATE' && request.userInfo.username == '%s' && request.userInfo.groups.exists(g, g == '%s') && !request.userInfo.groups.exists(g, g in ['system:masters','%s','%s']) && object.spec == oldObject.spec && object.metadata.labels['security.fs2.nebius.ai/retained-quarantine'] == 'true' && object.metadata.labels['security.fs2.nebius.ai/baseline-uid'] == oldObject.metadata.uid && object.metadata.labels['security.fs2.nebius.ai/baseline-object-sha256'].matches('^[a-f0-9]{64}$')) : true)",
            jsonencode(local.pod_security_legacy_cleanup_names.networkpolicies),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.daemonsets),
            jsonencode(local.pod_security_legacy_cleanup_names.networkpolicies),
            local.pod_security_custody_owner_username,
            local.pod_security_custody_owner_group,
            local.pod_security_platform_group,
            local.pod_security_rollout_custodian_group,
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            local.pod_security_custody_owner_username,
            local.pod_security_custody_owner_group,
            local.pod_security_platform_group,
            local.pod_security_rollout_custodian_group,
            jsonencode(local.pod_security_legacy_cleanup_names.daemonsets),
            local.pod_security_custody_owner_username,
            local.pod_security_custody_owner_group,
            local.pod_security_platform_group,
            local.pod_security_rollout_custodian_group,
          )
          message = "Legacy identities may only receive the exact custody-owner deny-only/tokenless quarantine update; deletion, recreation, DaemonSet mutation, and every later rewrite are denied."
        },
        {
          expression = format(
            "!variables.cleanupActive || request.operation == 'DELETE' || (object.kind == 'Pod' ? (!has(object.spec.serviceAccountName) || !(object.spec.serviceAccountName in %s)) : object.kind == 'PodTemplate' ? (!has(object.template.spec.serviceAccountName) || !(object.template.spec.serviceAccountName in %s)) : object.kind in ['Deployment','StatefulSet','DaemonSet','ReplicaSet','ReplicationController','Job'] ? (!has(object.spec.template.spec.serviceAccountName) || !(object.spec.template.spec.serviceAccountName in %s)) : object.kind == 'CronJob' ? (!has(object.spec.jobTemplate.spec.template.spec.serviceAccountName) || !(object.spec.jobTemplate.spec.template.spec.serviceAccountName in %s)) : object.kind == 'JobSet' ? object.spec.replicatedJobs.all(r, !has(r.template.spec.template.spec.serviceAccountName) || !(r.template.spec.template.spec.serviceAccountName in %s)) : object.kind == 'ModelDeployment' ? (!has(object.spec.template) || !has(object.spec.template.spec) || !has(object.spec.template.spec.serviceAccountName) || !(object.spec.template.spec.serviceAccountName in %s)) : true)",
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
          )
          message = "No Pod or supported controller may acquire a retained legacy ServiceAccount while quarantine is active."
        },
        {
          expression = format(
            "!variables.cleanupActive || request.operation == 'DELETE' || object.kind != 'Pod' || !has(object.metadata.ownerReferences) || object.metadata.ownerReferences.all(o, o.kind != 'DaemonSet' || !(o.name in %s))",
            jsonencode(local.pod_security_legacy_cleanup_names.daemonsets),
          )
          message = "Pods owned by a retained legacy DaemonSet are permanently quarantined."
        },
        {
          expression = format(
            "!variables.cleanupActive || object.kind != 'TokenRequest' || !(request.name in %s)",
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
          )
          message = "TokenRequest is forbidden for every retained legacy ServiceAccount."
        },
        {
          expression = format(
            "!variables.cleanupActive || object.kind != 'Secret' || (request.operation == 'DELETE' ? !(oldObject.type == 'kubernetes.io/service-account-token' && has(oldObject.metadata.annotations) && oldObject.metadata.annotations['kubernetes.io/service-account.name'] in %s) : !(object.type == 'kubernetes.io/service-account-token' && has(object.metadata.annotations) && object.metadata.annotations['kubernetes.io/service-account.name'] in %s))",
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
            jsonencode(local.pod_security_legacy_cleanup_names.serviceaccounts),
          )
          message = "Legacy ServiceAccount token Secrets may neither be created, changed, nor deleted during retained quarantine; any pre-existing credential blocks closure."
        },
      ]
    }
  }

  depends_on = [module.pod_security_rollout_gate]
}

resource "kubernetes_manifest" "pod_security_legacy_cleanup_fence_binding" {
  provider = kubernetes.pod_security_custody

  lifecycle {
    prevent_destroy = true
  }
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

# The rollout verifier advances this ConfigMap with a resourceVersion CAS using
# only the short-lived TokenRequest credential.  Requiring the authenticator's
# JTI credential-id denies ordinary --as impersonation even when an ambient
# principal can impersonate the service-account username.  The one legacy-v2
# edge atomically performs the signed predecessor adoption and next phase.
resource "kubernetes_manifest" "pod_security_ledger_policy" {
  provider = kubernetes.pod_security_custody
  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    terraform_data.cluster_contract,
  ]

  lifecycle {
    prevent_destroy = true
  }
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
          expression = "request.userInfo.username == '${local.pod_security_rollout_custodian_username}'"
          message    = "Only the Terraform-owned rollout-custodian service account may advance the pod-security ledger."
        },
        {
          expression = "has(request.userInfo.extra) && 'authentication.kubernetes.io/credential-id' in request.userInfo.extra && request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1 && request.userInfo.extra['authentication.kubernetes.io/credential-id'][0].startsWith('JTI=')"
          message    = "Rollout ledger writes require a real short-lived service-account JWT; impersonated usernames are denied."
        },
        {
          expression = "request.operation == 'DELETE' || (object.metadata.uid == oldObject.metadata.uid && object.metadata.name == oldObject.metadata.name && object.metadata.namespace == oldObject.metadata.namespace)"
          message    = "The rollout ledger identity is immutable."
        },
        {
          expression = "request.operation == 'DELETE' || (object.data.size() == 19 && ['schema','context_sha256','authority_key_id','authority_signer_identity','authority_public_key_sha256','sequence','state','last_bundle_sha256','last_receipt_id','last_nonce','proof_generation_ids','proof_generation_ledger_sha256','proof_generation_sequence','proof_generation_active','authorization_phase','authorization_bundle_sha256','authorization_nonce','authorization_owner_acknowledged','authorization_downstream_acknowledged'].all(k, k in object.data))"
          message    = "The rollout ledger must contain exactly the canonical admission-readable fields."
        },
        {
          expression = "request.operation == 'DELETE' || (object.data.schema == 'fs2-serve.nebius.ai/pod-security-rollout-ledger/v3' && object.data.authority_key_id == oldObject.data.authority_key_id && object.data.authority_signer_identity == oldObject.data.authority_signer_identity && object.data.authority_public_key_sha256 == oldObject.data.authority_public_key_sha256 && ((oldObject.data.schema == object.data.schema && object.data.context_sha256 == oldObject.data.context_sha256) || (oldObject.data.schema == 'fs2-serve.nebius.ai/pod-security-rollout-ledger/v2' && oldObject.data.size() == 15 && ['schema','context_sha256','authority_key_id','authority_signer_identity','authority_public_key_sha256','sequence','state','last_bundle_sha256','last_receipt_id','last_nonce','authorization_phase','authorization_bundle_sha256','authorization_nonce','authorization_owner_acknowledged','authorization_downstream_acknowledged'].all(k, k in oldObject.data))))"
          message    = "The rollout ledger context and signing authority are immutable except for the one signed v2-to-v3 adoption edge."
        },
        {
          expression = "request.operation == 'DELETE' || (object.data.proof_generation_ids.matches('^\\[\"[a-f0-9]{64}\"(,\"[a-f0-9]{64}\"){0,7}\\]$') && object.data.proof_generation_ledger_sha256.matches('^[a-f0-9]{64}$') && object.data.proof_generation_sequence.matches('^[1-8]$') && object.data.proof_generation_active.matches('^[a-f0-9]{64}$'))"
          message    = "The bounded proof-generation custody fields must be canonical."
        },
        {
          expression = "request.operation == 'DELETE' || oldObject.data.schema == 'fs2-serve.nebius.ai/pod-security-rollout-ledger/v2' || ((int(object.data.sequence) == int(oldObject.data.sequence) + 1 && int(object.data.proof_generation_sequence) >= int(oldObject.data.proof_generation_sequence) && (int(object.data.proof_generation_sequence) > int(oldObject.data.proof_generation_sequence) || (object.data.proof_generation_ids == oldObject.data.proof_generation_ids && object.data.proof_generation_ledger_sha256 == oldObject.data.proof_generation_ledger_sha256 && object.data.proof_generation_active == oldObject.data.proof_generation_active))) || (object.data.sequence == oldObject.data.sequence && object.data.proof_generation_ids == oldObject.data.proof_generation_ids && object.data.proof_generation_ledger_sha256 == oldObject.data.proof_generation_ledger_sha256 && object.data.proof_generation_sequence == oldObject.data.proof_generation_sequence && object.data.proof_generation_active == oldObject.data.proof_generation_active))"
          message    = "Proof-generation custody may append only with a phase transition and is immutable during acknowledgements."
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

}

resource "kubernetes_manifest" "pod_security_ledger_binding" {
  provider = kubernetes.pod_security_custody
  depends_on = [
    kubernetes_manifest.pod_security_custody_boundary_binding,
    kubernetes_manifest.pod_security_ledger_policy,
  ]

  lifecycle {
    prevent_destroy = true
  }
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

}
# END RETAINED PLATFORM-STATE CUSTODY DECLARATIONS.
