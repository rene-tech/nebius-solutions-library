locals {
  snapshot_exception_namespace = "fs2-snapshot-operations"
  snapshot_manager_username    = "system:serviceaccount:fs2-system:fs2-snapshot-manager"
  snapshot_job_controller      = "system:serviceaccount:kube-system:job-controller"
  snapshot_profile             = "esmfold2-h100-v1"
  snapshot_runtime_image       = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3"
  snapshot_tools_image         = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4"

  snapshot_fixed_pod_expression = join(" && ", [
    "object.metadata.namespace == '${local.snapshot_exception_namespace}'",
    "object.metadata.name.startsWith('fs2-snapshot-')",
    "object.metadata.labels['security.fs2.nebius.ai/snapshot-profile'] == '${local.snapshot_profile}'",
    "object.spec.serviceAccountName == 'fs2-snapshot-runtime'",
    "object.spec.automountServiceAccountToken == false",
    "object.spec.restartPolicy == 'Never'",
    "object.spec.terminationGracePeriodSeconds == 10",
    "object.spec.hostNetwork == false && object.spec.hostPID == false && object.spec.hostIPC == false",
    "object.spec.securityContext == {'fsGroup':65532,'fsGroupChangePolicy':'OnRootMismatch','seccompProfile':{'type':'RuntimeDefault'}}",
    "object.spec.nodeSelector.size() == 1 && object.spec.nodeSelector['kubernetes.io/hostname'] != ''",
    "object.spec.tolerations.size() == 1 && object.spec.tolerations[0] == {'key':'dedicated','operator':'Equal','value':'fs2-inference','effect':'NoSchedule'}",
    "object.spec.volumes.size() == 4",
    "object.spec.volumes.exists(v, v.name == 'tools' && has(v.emptyDir))",
    "object.spec.volumes.exists(v, v.name == 'checkpoints' && v.persistentVolumeClaim.claimName == 'fs2-snapshot-checkpoints' && (!has(v.persistentVolumeClaim.readOnly) || v.persistentVolumeClaim.readOnly == false))",
    "object.spec.volumes.exists(v, v.name == 'reference' && v.persistentVolumeClaim.claimName == 'fs2-snapshot-reference' && v.persistentVolumeClaim.readOnly == true)",
    "object.spec.volumes.exists(v, v.name == 'shm' && v.emptyDir.medium == 'Memory' && v.emptyDir.sizeLimit == quantity('8Gi'))",
    "object.spec.initContainers.size() == 1",
    "object.spec.initContainers[0].name == 'snapshot-tools'",
    "object.spec.initContainers[0].image == '${local.snapshot_tools_image}'",
    "object.spec.initContainers[0].imagePullPolicy == 'IfNotPresent'",
    "object.spec.initContainers[0].command == ['/bin/sh','-c','cp -a /snapshot-binaries/. /tools/ && cp -L /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/']",
    "object.spec.initContainers[0].volumeMounts == [{'name':'tools','mountPath':'/tools'}]",
    "object.spec.initContainers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':true,'runAsNonRoot':true,'runAsUser':65532,'runAsGroup':65532,'seccompProfile':{'type':'RuntimeDefault'}}",
    "!has(object.spec.initContainers[0].args) && !has(object.spec.initContainers[0].env) && !has(object.spec.initContainers[0].ports) && !has(object.spec.initContainers[0].lifecycle) && !has(object.spec.initContainers[0].livenessProbe) && !has(object.spec.initContainers[0].readinessProbe) && !has(object.spec.initContainers[0].startupProbe)",
    "object.spec.containers.size() == 1",
    "object.spec.containers[0].name == 'runtime'",
    "object.spec.containers[0].image == '${local.snapshot_runtime_image}'",
    "object.spec.containers[0].imagePullPolicy == 'IfNotPresent'",
    "object.spec.containers[0].securityContext == {'privileged':true,'runAsUser':0,'runAsGroup':0}",
    "object.spec.containers[0].resources.requests == {'cpu':quantity('8'),'memory':quantity('96Gi'),'nvidia.com/gpu':quantity('1')} && object.spec.containers[0].resources.limits == {'cpu':quantity('16'),'memory':quantity('128Gi'),'nvidia.com/gpu':quantity('1')}",
    "object.spec.containers[0].volumeMounts.size() == 6",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'tools' && m.mountPath == '/tools')",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'checkpoints' && m.mountPath == '/checkpoints' && (!has(m.readOnly) || m.readOnly == false))",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'shm' && m.mountPath == '/dev/shm')",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'reference' && m.mountPath == '/models/esmfold2' && m.subPath == 'model-artifacts/public/v1/objects/esmfold2-trunk/sha256/136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c' && m.readOnly == true)",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'reference' && m.mountPath == '/models/esmc-6b' && m.subPath == 'model-artifacts/public/v1/objects/esmc-6b/sha256/8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758' && m.readOnly == true)",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'reference' && m.mountPath == '/databases/esmfold2' && m.subPath == 'model-artifacts/public/v1/objects/esmfold2-ccd/sha256/b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e' && m.readOnly == true)",
    "object.spec.containers[0].env.size() == 5",
    "object.spec.containers[0].env.exists(e, e.name == 'FS2_SNAPSHOT_RUNTIME_IMAGE' && e.value == '${local.snapshot_runtime_image}')",
    "object.spec.containers[0].env.exists(e, e.name == 'FS2_SNAPSHOT_TOOLS_IMAGE' && e.value == '${local.snapshot_tools_image}')",
    "object.spec.containers[0].env.exists(e, e.name == 'HF_HUB_OFFLINE' && e.value == '1')",
    "object.spec.containers[0].env.exists(e, e.name == 'TRANSFORMERS_OFFLINE' && e.value == '1')",
    "object.spec.containers[0].env.exists(e, e.name == 'ESMCFOLD_CCD_PATH' && e.value == '/databases/esmfold2/ccd.pkl')",
    "object.spec.containers[0].command.size() in [14,18]",
    "object.spec.containers[0].command[0:8] == ['/bin/bash','-c','source \"$1\"; shift; exec \"$@\"','fs2-snapshot','/opt/fs2/activate.sh','/opt/esm/.pixi/envs/gpu/bin/python','/opt/fs2/snapshot/supervisor.py','--directory']",
    "object.spec.containers[0].command[8].matches('^/checkpoints/[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$')",
    "object.spec.containers[0].command[9:13] == ['--request-uid','10001','--request-gid','10001']",
    "(object.spec.containers[0].command.size() == 14 && object.spec.containers[0].command[13] == 'restore') || (object.spec.containers[0].command.size() == 18 && object.spec.containers[0].command[13:18] == ['donor','--','/opt/esm/.pixi/envs/gpu/bin/python','-u','/opt/fs2/snapshot/esmfold2_server.py'])",
    "!has(object.spec.containers[0].args) && !has(object.spec.containers[0].ports) && !has(object.spec.containers[0].lifecycle) && !has(object.spec.containers[0].livenessProbe) && !has(object.spec.containers[0].readinessProbe) && !has(object.spec.containers[0].startupProbe)",
    "!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.size() == 0",
  ])
  snapshot_durability_pod_expression = join(" && ", [
    "object.metadata.namespace == '${local.snapshot_exception_namespace}'",
    # Job-controller generated Pod names may truncate a 54-character Job name
    # before adding their random suffix. The exact, untruncated Job identity is
    # still bound below by ownerReference, job-name label and proof-mode.
    "object.metadata.name.startsWith('fs2-snapshot-checkpoints-durability-') && object.metadata.name.matches('^[a-z0-9](?:[-a-z0-9]{0,55}[a-z0-9])?-[a-z0-9]{5}$')",
    "object.metadata.ownerReferences.size() == 1",
    "object.metadata.ownerReferences[0].apiVersion == 'batch/v1' && object.metadata.ownerReferences[0].kind == 'Job' && object.metadata.ownerReferences[0].controller == true",
    "object.metadata.ownerReferences[0].name.matches('^fs2-snapshot-checkpoints-durability-(write|read)-[a-f0-9]{12}$')",
    "object.metadata.labels['batch.kubernetes.io/job-name'] == object.metadata.ownerReferences[0].name",
    "object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] in ['write','read']",
    "object.metadata.annotations['security.fs2.nebius.ai/proof-generation'] == '${var.pod_security_storage_proof_generation}'",
    "object.metadata.annotations['security.fs2.nebius.ai/proof-attempt'] == '${var.pod_security_storage_proof_attempt}'",
    "object.metadata.ownerReferences[0].name.endsWith('-${substr(var.pod_security_storage_proof_generation, 0, 12)}')",
    "object.metadata.ownerReferences[0].name.startsWith('fs2-snapshot-checkpoints-durability-' + object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] + '-')",
    "object.spec.serviceAccountName == 'default' && object.spec.automountServiceAccountToken == false",
    "object.spec.restartPolicy == 'Never' && object.spec.enableServiceLinks == false",
    "(!has(object.spec.hostNetwork) || object.spec.hostNetwork == false) && (!has(object.spec.hostPID) || object.spec.hostPID == false) && (!has(object.spec.hostIPC) || object.spec.hostIPC == false)",
    "object.spec.nodeSelector['storage.fs2.nebius/reference-data'] == 'true'",
    "object.spec.securityContext.runAsNonRoot == true && object.spec.securityContext.runAsUser == 65532 && object.spec.securityContext.runAsGroup == 65532 && object.spec.securityContext.fsGroup == 65532 && object.spec.securityContext.seccompProfile.type == 'RuntimeDefault'",
    "object.spec.volumes.size() == 2",
    "object.spec.volumes.exists(v, v.name == 'checkpoints' && v.persistentVolumeClaim.claimName == 'fs2-snapshot-checkpoints' && ((object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] == 'read' && has(v.persistentVolumeClaim.readOnly) && v.persistentVolumeClaim.readOnly == true) || (object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] == 'write' && (!has(v.persistentVolumeClaim.readOnly) || v.persistentVolumeClaim.readOnly == false))))",
    "object.spec.volumes.exists(v, v.name == 'tools' && v.configMap.name == '${var.pod_security_storage_tools_config_map}')",
    "!has(object.spec.initContainers) || object.spec.initContainers.size() == 0",
    "!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.size() == 0",
    "object.spec.containers.size() == 1",
    "object.spec.containers[0].name == 'durability-proof' && object.spec.containers[0].image == '${var.pod_security_storage_probe_image}' && object.spec.containers[0].imagePullPolicy == 'IfNotPresent'",
    "object.spec.containers[0].command.size() == 19",
    "object.spec.containers[0].command[0:5] == ['python','/opt/fs2/reference-data/verify_checkpoint_durability.py',object.metadata.annotations['security.fs2.nebius.ai/proof-mode'],'--root','/checkpoints']",
    "object.spec.containers[0].command[5:19] == ['--pvc-uid',object.metadata.annotations['security.fs2.nebius.ai/pvc-uid'],'--pvc-resource-version',object.metadata.annotations['security.fs2.nebius.ai/pvc-resource-version'],'--volume-name',object.metadata.annotations['security.fs2.nebius.ai/volume-name'],'--challenge',object.metadata.annotations['security.fs2.nebius.ai/proof-challenge'],'--generation',object.metadata.annotations['security.fs2.nebius.ai/proof-generation'],'--attempt',object.metadata.annotations['security.fs2.nebius.ai/proof-attempt'],'--proof-output','/dev/termination-log']",
    "object.spec.containers[0].terminationMessagePath == '/dev/termination-log' && object.spec.containers[0].terminationMessagePolicy == 'File'",
    "object.spec.containers[0].resources.requests == {'cpu':quantity('50m'),'memory':quantity('64Mi'),'ephemeral-storage':quantity('64Mi')} && object.spec.containers[0].resources.limits == {'cpu':quantity('250m'),'memory':quantity('256Mi'),'ephemeral-storage':quantity('256Mi')}",
    "object.spec.containers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':true}",
    "object.spec.containers[0].volumeMounts.size() == 2",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'checkpoints' && m.mountPath == '/checkpoints' && ((object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] == 'read' && has(m.readOnly) && m.readOnly == true) || (object.metadata.annotations['security.fs2.nebius.ai/proof-mode'] == 'write' && (!has(m.readOnly) || m.readOnly == false))))",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'tools' && m.mountPath == '/opt/fs2/reference-data' && m.readOnly == true)",
    "!has(object.spec.containers[0].args) && !has(object.spec.containers[0].env) && !has(object.spec.containers[0].ports) && !has(object.spec.containers[0].lifecycle) && !has(object.spec.containers[0].livenessProbe) && !has(object.spec.containers[0].readinessProbe) && !has(object.spec.containers[0].startupProbe)",
  ])
  snapshot_reference_probe_pod_expression = join(" && ", [
    "object.metadata.namespace == '${local.snapshot_exception_namespace}'",
    # The exact generation is retained in the untruncated ownerReference and
    # job-name label even when Job controller truncates the Pod generateName.
    "object.metadata.name.startsWith('fs2-snapshot-reference-read-probe-') && object.metadata.name.matches('^[a-z0-9](?:[-a-z0-9]{0,55}[a-z0-9])?-[a-z0-9]{5}$')",
    "object.metadata.ownerReferences.size() == 1",
    "object.metadata.ownerReferences[0].apiVersion == 'batch/v1' && object.metadata.ownerReferences[0].kind == 'Job' && object.metadata.ownerReferences[0].controller == true",
    "object.metadata.ownerReferences[0].name.matches('^fs2-snapshot-reference-read-probe-[a-f0-9]{12}$')",
    "object.metadata.labels['batch.kubernetes.io/job-name'] == object.metadata.ownerReferences[0].name",
    "object.metadata.annotations['security.fs2.nebius.ai/proof-generation'] == '${var.pod_security_storage_proof_generation}'",
    "object.metadata.annotations['security.fs2.nebius.ai/proof-attempt'] == '${var.pod_security_storage_proof_attempt}'",
    "object.metadata.ownerReferences[0].name.endsWith('-${substr(var.pod_security_storage_proof_generation, 0, 12)}')",
    "object.spec.serviceAccountName == 'default' && object.spec.automountServiceAccountToken == false",
    "object.spec.restartPolicy == 'Never' && object.spec.enableServiceLinks == false",
    "(!has(object.spec.hostNetwork) || object.spec.hostNetwork == false) && (!has(object.spec.hostPID) || object.spec.hostPID == false) && (!has(object.spec.hostIPC) || object.spec.hostIPC == false)",
    "object.spec.nodeSelector['storage.fs2.nebius/reference-data'] == 'true'",
    "object.spec.securityContext.runAsNonRoot == true && object.spec.securityContext.runAsUser == 65532 && object.spec.securityContext.runAsGroup == 65532 && object.spec.securityContext.seccompProfile.type == 'RuntimeDefault'",
    "object.spec.volumes.size() == 3",
    "object.spec.volumes.exists(v, v.name == 'reference-data' && v.persistentVolumeClaim.claimName == 'fs2-snapshot-reference' && v.persistentVolumeClaim.readOnly == true)",
    "object.spec.volumes.exists(v, v.name == 'tools' && v.configMap.name == '${var.pod_security_storage_tools_config_map}')",
    "object.spec.volumes.exists(v, v.name == 'tmp' && v.emptyDir.sizeLimit == quantity('64Mi'))",
    "!has(object.spec.initContainers) || object.spec.initContainers.size() == 0",
    "!has(object.spec.ephemeralContainers) || object.spec.ephemeralContainers.size() == 0",
    "object.spec.containers.size() == 1",
    "object.spec.containers[0].name == 'read-probe' && object.spec.containers[0].image == '${var.pod_security_storage_probe_image}' && object.spec.containers[0].imagePullPolicy == 'IfNotPresent'",
    "object.spec.containers[0].command.size() == 26",
    "object.spec.containers[0].command == ['python','/opt/fs2/reference-data/verify_csi_readiness.py','--root','/reference-data','--receipt',object.metadata.annotations['reference-data.fs2.nebius.ai/receipt'],'--bundle',object.metadata.annotations['reference-data.fs2.nebius.ai/bundle'],'--revision',object.metadata.annotations['reference-data.fs2.nebius.ai/revision'],'--tree-sha256',object.metadata.annotations['reference-data.fs2.nebius.ai/tree-sha256'],'--pvc-uid',object.metadata.annotations['reference-data.fs2.nebius.ai/pvc-uid'],'--pvc-resource-version',object.metadata.annotations['reference-data.fs2.nebius.ai/pvc-resource-version'],'--volume-name',object.metadata.annotations['reference-data.fs2.nebius.ai/volume-name'],'--challenge',object.metadata.annotations['reference-data.fs2.nebius.ai/proof-challenge'],'--generation',object.metadata.annotations['security.fs2.nebius.ai/proof-generation'],'--attempt',object.metadata.annotations['security.fs2.nebius.ai/proof-attempt'],'--proof-output','/dev/termination-log']",
    "object.metadata.annotations['security.fs2.nebius.ai/verified-tree-sha256'] == object.metadata.annotations['reference-data.fs2.nebius.ai/tree-sha256']",
    "object.spec.containers[0].terminationMessagePath == '/dev/termination-log' && object.spec.containers[0].terminationMessagePolicy == 'File'",
    "object.spec.containers[0].resources.requests == {'cpu':quantity('50m'),'memory':quantity('64Mi'),'ephemeral-storage':quantity('64Mi')} && object.spec.containers[0].resources.limits == {'cpu':quantity('250m'),'memory':quantity('256Mi'),'ephemeral-storage':quantity('256Mi')}",
    "object.spec.containers[0].securityContext == {'allowPrivilegeEscalation':false,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':true}",
    "object.spec.containers[0].volumeMounts.size() == 3",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'reference-data' && m.mountPath == '/reference-data' && m.readOnly == true)",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'tools' && m.mountPath == '/opt/fs2/reference-data' && m.readOnly == true)",
    "object.spec.containers[0].volumeMounts.exists(m, m.name == 'tmp' && m.mountPath == '/tmp')",
    "!has(object.spec.containers[0].args) && !has(object.spec.containers[0].env) && !has(object.spec.containers[0].ports) && !has(object.spec.containers[0].lifecycle) && !has(object.spec.containers[0].livenessProbe) && !has(object.spec.containers[0].readinessProbe) && !has(object.spec.containers[0].startupProbe)",
  ])
}

# This identity is the only namespaced writer. It is tokenless by default; an
# operator obtains a short-lived TokenRequest credential for an approved run.
resource "kubernetes_service_account_v1" "snapshot_manager" {
  count = local.node_observability_exception_enabled ? 1 : 0
  metadata {
    name      = "fs2-snapshot-manager"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  automount_service_account_token = false
}

resource "kubernetes_service_account_v1" "snapshot_runtime" {
  count = local.node_observability_exception_enabled ? 1 : 0
  metadata {
    name      = "fs2-snapshot-runtime"
    namespace = local.snapshot_exception_namespace
    labels    = local.common_labels
  }
  automount_service_account_token = false
  depends_on                      = [kubernetes_namespace_v1.platform]
}

resource "kubernetes_role_v1" "snapshot_manager" {
  count = local.node_observability_exception_enabled ? 1 : 0
  metadata {
    name      = "fs2-snapshot-manager"
    namespace = local.snapshot_exception_namespace
    labels    = local.common_labels
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["create", "delete", "get", "list", "watch"]
  }
  rule {
    api_groups     = [""]
    resources      = ["persistentvolumeclaims"]
    resource_names = ["fs2-snapshot-checkpoints", "fs2-snapshot-reference"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "snapshot_manager" {
  count = local.node_observability_exception_enabled ? 1 : 0
  metadata {
    name      = "fs2-snapshot-manager"
    namespace = local.snapshot_exception_namespace
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.snapshot_manager[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.snapshot_manager[0].metadata[0].name
    namespace = kubernetes_service_account_v1.snapshot_manager[0].metadata[0].namespace
  }
}

resource "kubernetes_manifest" "snapshot_pod_policy" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata   = { name = "fs2-snapshot-exact-profile", labels = local.common_labels }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          { apiGroups = [""], apiVersions = ["v1"], operations = ["CREATE", "UPDATE"], resources = ["pods"] },
          { apiGroups = [""], apiVersions = ["v1"], operations = ["CREATE", "UPDATE"], resources = ["pods/ephemeralcontainers"] },
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
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "snapshot_pod_binding" {
  count = local.node_observability_exception_enabled ? 1 : 0
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata   = { name = "fs2-snapshot-exact-profile", labels = local.common_labels }
    spec = {
      policyName        = "fs2-snapshot-exact-profile"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = { matchLabels = { "security.fs2.nebius.ai/snapshot-only" = "true" } }
      }
    }
  }
  depends_on = [kubernetes_manifest.snapshot_pod_policy, kubernetes_namespace_v1.platform]
}

resource "kubernetes_network_policy_v1" "snapshot_default_deny" {
  count = local.node_observability_exception_enabled ? 1 : 0
  metadata {
    name      = "fs2-snapshot-default-deny"
    namespace = local.snapshot_exception_namespace
    labels    = local.common_labels
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
  depends_on = [kubernetes_namespace_v1.platform]
}
