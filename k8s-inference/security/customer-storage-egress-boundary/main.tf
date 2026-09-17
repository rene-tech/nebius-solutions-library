locals {
  namespace = "fs2-system"

  current_contract = var.contract_generations[var.current_generation]
  current_trust    = var.trust_generations[local.current_contract.trust_generation]

  contract_digests = {
    for generation, contract in var.contract_generations :
    generation => sha256(jsonencode(jsondecode(contract.contract_json)))
  }
  legacy_contracts = {
    for generation, contract in var.contract_generations : generation => contract
    if contains(var.legacy_contract_generations, generation)
  }
  successor_contracts = {
    for generation, contract in var.contract_generations : generation => contract
    if !contains(var.legacy_contract_generations, generation)
  }
  legacy_trusts = {
    for generation, trust in var.trust_generations : generation => trust
    if contains(var.legacy_trust_generations, generation)
  }
  successor_trusts = {
    for generation, trust in var.trust_generations : generation => trust
    if !contains(var.legacy_trust_generations, generation)
  }
  contract_names = {
    for generation in keys(local.legacy_contracts) :
    generation => "fs2-customer-storage-egress-contract-${generation}"
  }
  successor_contract_names = {
    for generation in keys(local.successor_contracts) :
    generation => "fs2-storage-v3-contract-${generation}"
  }
  trust_names = {
    for generation in keys(local.legacy_trusts) :
    generation => "fs2-customer-storage-egress-trust-${generation}"
  }
  successor_trust_names = {
    for generation in keys(local.successor_trusts) :
    generation => "fs2-storage-v3-trust-${generation}"
  }
  network_policy_names = {
    for generation in keys(local.legacy_contracts) :
    generation => "fs2-customer-storage-egress-${generation}"
  }
  successor_network_policy_names = {
    for generation in keys(local.successor_contracts) :
    generation => "fs2-storage-v3-network-policy-${generation}"
  }
  boundary_policy_names = {
    for generation in var.legacy_boundary_generations :
    generation => "fs2-customer-storage-egress-boundary-${generation}"
  }
  successor_boundary_policy_names = {
    for generation in keys(var.successor_boundary_generations) :
    generation => "fs2-storage-v3-boundary-${generation}"
  }
  workload_policy_names = {
    for generation in var.legacy_workload_policy_generations :
    generation => "fs2-customer-storage-egress-boundary-workload-${generation}"
  }
  successor_workload_policy_names = {
    for generation in keys(var.successor_workload_policy_generations) :
    generation => "fs2-storage-v3-workload-${generation}"
  }
  v3_pod_labels = {
    "app.kubernetes.io/name"                   = "fs2-serve-control-plane"
    "app.kubernetes.io/instance"               = "fs2-serve-control-plane"
    "app.kubernetes.io/component"              = "storage-reconciler-v3"
    "fs2.nebius.ai/storage-egress-generation"  = var.current_generation
    "fs2.nebius.ai/storage-rollout-generation" = var.current_release_generation
  }
  v3_pod_labels_cel        = jsonencode(local.v3_pod_labels)
  v3_runtime_label_key_cel = "pod-template-hash"
  v3_selector_matches_object_cel = join(" ", [
    "(!has(object.spec.podSelector.matchLabels) ||",
    "object.spec.podSelector.matchLabels.all(key, value,",
    "(key in ${local.v3_pod_labels_cel} && ${local.v3_pod_labels_cel}[key] == value) || key == '${local.v3_runtime_label_key_cel}')) &&",
    "(!has(object.spec.podSelector.matchExpressions) ||",
    "object.spec.podSelector.matchExpressions.all(term,",
    "(term.operator == 'In' && ((term.key in ${local.v3_pod_labels_cel} && term.values.exists(value, ${local.v3_pod_labels_cel}[term.key] == value)) || (term.key == '${local.v3_runtime_label_key_cel}' && size(term.values) > 0))) ||",
    "(term.operator == 'NotIn' && (term.key == '${local.v3_runtime_label_key_cel}' || !(term.key in ${local.v3_pod_labels_cel}) || !term.values.exists(value, ${local.v3_pod_labels_cel}[term.key] == value))) ||",
    "(term.operator == 'Exists' && (term.key in ${local.v3_pod_labels_cel} || term.key == '${local.v3_runtime_label_key_cel}')) ||",
    "(term.operator == 'DoesNotExist' && !(term.key in ${local.v3_pod_labels_cel}) && term.key != '${local.v3_runtime_label_key_cel}'))) ",
  ])
  v3_selector_matches_old_object_cel = replace(
    local.v3_selector_matches_object_cel,
    "object.spec.podSelector",
    "oldObject.spec.podSelector",
  )
  current_network_policy_spec = {
    podSelector = { matchLabels = {
      "app.kubernetes.io/name"                  = "fs2-serve-control-plane"
      "app.kubernetes.io/instance"              = "fs2-serve-control-plane"
      "app.kubernetes.io/component"             = "storage-reconciler-v3"
      "fs2.nebius.ai/storage-egress-generation" = var.current_generation
    } }
    policyTypes = ["Ingress", "Egress"]
    ingress     = []
    egress = [
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
          podSelector = { matchLabels = {
            "app.kubernetes.io/instance" = "coredns"
            "app.kubernetes.io/name"     = "coredns"
            "k8s-app"                    = "coredns"
          } }
        }]
        ports = [{ port = 53, protocol = "UDP" }, { port = 53, protocol = "TCP" }]
      },
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-data" } }
          podSelector       = { matchLabels = { "cnpg.io/cluster" = "fs2-control-db" } }
        }]
        ports = [{ port = 5432, protocol = "TCP" }]
      },
      {
        to    = [for cidr in sort(jsondecode(local.current_contract.contract_json).cidrs) : { ipBlock = { cidr = cidr } }]
        ports = [{ port = 443, protocol = "TCP" }]
      },
      {
        to    = [for cidr in sort(tolist(local.current_contract.kubernetes_api_cidrs)) : { ipBlock = { cidr = cidr } }]
        ports = [{ port = 443, protocol = "TCP" }]
      },
    ]
  }
  boundary_policy_spec = {
    failurePolicy = "Fail"
    matchPolicy   = "Equivalent"
    matchConstraints = {
      resourceRules = [
        {
          apiGroups   = ["networking.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["networkpolicies"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["configmaps"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = ["admissionregistration.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
          scope       = "Cluster"
        },
        {
          apiGroups   = ["rbac.authorization.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["roles", "rolebindings"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["deployments"]
          scope       = "Namespaced"
        },
      ]
    }
    matchConditions = [{
      name = "customer-storage-security-boundary"
      expression = join(" ", [
        "(request.resource.group == 'admissionregistration.k8s.io' &&",
        "request.name in ['${local.successor_boundary_policy_names[var.current_boundary_generation]}','${local.successor_workload_policy_names[var.current_workload_policy_generation]}']) ||",
        "(request.resource.group == 'rbac.authorization.k8s.io' && request.namespace == '${local.namespace}' &&",
        "request.name == '${local.current_release_name}') ||",
        "(request.namespace == '${local.namespace}' &&",
        "request.name in ['${local.successor_contract_names[var.current_generation]}','${local.successor_trust_names[local.current_contract.trust_generation]}']) ||",
        "(request.resource.group == 'networking.k8s.io' && request.namespace == '${local.namespace}' &&",
        "(request.operation == 'DELETE' ?",
        "(oldObject.metadata.name == '${local.successor_network_policy_names[var.current_generation]}' || (${local.v3_selector_matches_old_object_cel})) :",
        "(object.metadata.name == '${local.successor_network_policy_names[var.current_generation]}' || (${local.v3_selector_matches_object_cel}))))",
      ])
    }]
    validations = [
      {
        expression = join(" ", [
          "request.resource.group != 'networking.k8s.io' ||",
          "(request.operation == 'CREATE' &&",
          "object.metadata.name == '${local.successor_network_policy_names[var.current_generation]}' &&",
          "has(object.metadata.annotations) &&",
          "object.metadata.annotations['fs2.nebius.ai/storage-egress-contract-sha256'] == '${local.contract_digests[var.current_generation]}' &&",
          "object.spec == ${jsonencode(local.current_network_policy_spec)})",
        ])
        message = "Only the exact externally bound NetworkPolicy may enter the frozen effective union."
        reason  = "Forbidden"
      },
      {
        expression = "request.resource.group == 'networking.k8s.io' || request.operation == 'CREATE'"
        message    = "Customer-storage egress security generations are create-only and cannot be updated or deleted."
        reason     = "Forbidden"
      },
      {
        expression = "request.userInfo.groups.exists(group, group == '${var.security_owner_group}')"
        message    = "Only the separately authenticated customer-storage security owner may change this boundary."
        reason     = "Forbidden"
      },
    ]
  }
  boundary_policy_sha256 = sha256(jsonencode(local.boundary_policy_spec))
  legacy_releases = {
    for generation, release in var.release_generations : generation => release
    if contains(var.legacy_release_generations, generation)
  }
  successor_releases = {
    for generation, release in var.release_generations : generation => release
    if !contains(var.legacy_release_generations, generation)
  }
  legacy_release_names = {
    for generation in keys(local.legacy_releases) :
    generation => "fs2-storage-v2-${element(reverse(split("-", var.current_generation)), 0)}-${element(reverse(split("-", generation)), 0)}"
  }
  release_names = {
    for generation in keys(local.successor_releases) :
    generation => "fs2-storage-v3-${element(reverse(split("-", var.current_generation)), 0)}-${element(reverse(split("-", generation)), 0)}"
  }
  release_payloads = {
    for generation, release in var.release_generations : generation => {
      image_repository                 = release.image_repository
      image_digest                     = release.image_digest
      image_pull_secrets               = release.image_pull_secrets
      storage_project_id               = release.storage_project_id
      storage_region                   = release.storage_region
      quota_bytes                      = release.quota_bytes
      excluded_tenants                 = sort(tolist(release.excluded_tenants))
      resource_credentials_secret_name = release.resource_credentials_secret_name
      iam_credentials_secret_name      = release.iam_credentials_secret_name
      database_secret_name             = release.database_secret_name
      crypto_secret_name               = release.crypto_secret_name
      storage_generation               = release.storage_generation
      key_ttl_days                     = release.key_ttl_days
      rotation_window_days             = release.rotation_window_days
      action_timeout_seconds           = release.action_timeout_seconds
    }
  }
  current_release      = var.release_generations[var.current_release_generation]
  current_release_name = local.release_names[var.current_release_generation]
  allowed_secret_names = sort([
    local.current_release.crypto_secret_name,
    local.current_release.database_secret_name,
    local.current_release.resource_credentials_secret_name,
    local.current_release.iam_credentials_secret_name,
  ])
  allowed_secret_names_cel = jsonencode(local.allowed_secret_names)
  expected_pull_secrets_cel = jsonencode([
    for name in local.current_release.image_pull_secrets : { name = name }
  ])
  expected_container_env_cel = jsonencode([
    { name = "FS2_DATABASE_URL", valueFrom = { secretKeyRef = { name = local.current_release.database_secret_name, key = "url" } } },
    { name = "FS2_USER_STORAGE_KEYRING_FILE", value = "/var/run/secrets/fs2-serve/customer-storage-crypto/keyring.json" },
    { name = "FS2_USER_STORAGE_NAME_KEYRING_FILE", value = "/var/run/secrets/fs2-serve/customer-storage-crypto/name-keyring.json" },
    { name = "FS2_USER_STORAGE_ENABLED", value = "true" },
    { name = "FS2_USER_STORAGE_PROJECT_ID", value = local.current_release.storage_project_id },
    { name = "FS2_USER_STORAGE_REGION", value = local.current_release.storage_region },
    { name = "FS2_USER_STORAGE_DEFAULT_MODE", value = "user" },
    { name = "FS2_USER_STORAGE_QUOTA_BYTES", value = tostring(local.current_release.quota_bytes) },
    { name = "FS2_USER_STORAGE_EXCLUDED_TENANTS", value = jsonencode(sort(tolist(local.current_release.excluded_tenants))) },
    { name = "FS2_USER_STORAGE_POLL_SECONDS", value = "5" },
    { name = "FS2_USER_STORAGE_KEY_TTL_DAYS", value = tostring(local.current_release.key_ttl_days) },
    { name = "FS2_USER_STORAGE_ROTATION_WINDOW_DAYS", value = tostring(local.current_release.rotation_window_days) },
    { name = "FS2_USER_STORAGE_ACTION_TIMEOUT_SECONDS", value = tostring(local.current_release.action_timeout_seconds) },
    { name = "FS2_USER_STORAGE_RESOURCE_CREDENTIALS_FILE", value = "/var/run/secrets/fs2-serve/customer-storage/resource/credentials.json" },
    { name = "FS2_USER_STORAGE_IAM_CREDENTIALS_FILE", value = "/var/run/secrets/fs2-serve/customer-storage/iam/credentials.json" },
  ])
  expected_init_args_cel = jsonencode([
    "--contract", "/verify/contract.json",
    "--public-key", "/verify/public-key.pem",
    "--expected-kubernetes-api-cidrs", "/verify/kubernetes-api-cidrs.json",
    "--kubernetes-network-policy-set", local.namespace,
    "--pod-label", "app.kubernetes.io/name=fs2-serve-control-plane",
    "--pod-label", "app.kubernetes.io/instance=fs2-serve-control-plane",
    "--pod-label", "app.kubernetes.io/component=storage-reconciler-v3",
    "--pod-label", "fs2.nebius.ai/storage-egress-generation=${var.current_generation}",
    "--pod-label", "fs2.nebius.ai/storage-rollout-generation=${var.current_release_generation}",
    "--pod-label-from-env", "pod-template-hash=FS2_POD_TEMPLATE_HASH",
  ])
  expected_container_mounts_cel = jsonencode([
    { name = "customer-storage-crypto", mountPath = "/var/run/secrets/fs2-serve/customer-storage-crypto", readOnly = true },
    { name = "database-ca", mountPath = "/tls", readOnly = true },
    { name = "customer-storage-resource", mountPath = "/var/run/secrets/fs2-serve/customer-storage/resource", readOnly = true },
    { name = "customer-storage-iam", mountPath = "/var/run/secrets/fs2-serve/customer-storage/iam", readOnly = true },
  ])
  expected_init_env_cel = jsonencode([
    {
      name = "FS2_POD_TEMPLATE_HASH"
      valueFrom = {
        fieldRef = {
          apiVersion = "v1"
          fieldPath  = "metadata.labels['pod-template-hash']"
        }
      }
    },
  ])
  expected_init_mounts_cel = jsonencode([
    { name = "egress-contract", mountPath = "/verify/contract.json", subPath = "contract.json", readOnly = true },
    { name = "egress-trust", mountPath = "/verify/public-key.pem", subPath = "public-key.pem", readOnly = true },
    { name = "egress-contract", mountPath = "/verify/kubernetes-api-cidrs.json", subPath = "kubernetes-api-cidrs.json", readOnly = true },
    { name = "kubernetes-api", mountPath = "/var/run/secrets/kubernetes.io/serviceaccount", readOnly = true },
  ])
  workload_pod_spec_template_cel = join(" ", [
    "POD.serviceAccountName == '${local.current_release_name}' &&",
    "has(POD.automountServiceAccountToken) && POD.automountServiceAccountToken == false &&",
    "has(POD.enableServiceLinks) && POD.enableServiceLinks == false &&",
    "POD.nodeSelector == {'${var.provider_authority.node_selector_key}':'${var.provider_authority.node_selector_value}'} &&",
    "(!has(POD.nodeName) || POD.nodeName == '') &&",
    "size(POD.tolerations) == 1 && POD.tolerations[0].key == '${var.provider_authority.taint_key}' &&",
    "POD.tolerations[0].operator == 'Equal' && POD.tolerations[0].value == '${var.provider_authority.taint_value}' &&",
    "POD.tolerations[0].effect == '${var.provider_authority.taint_effect}' &&",
    "(!has(POD.hostNetwork) || POD.hostNetwork == false) &&",
    "(!has(POD.hostPID) || POD.hostPID == false) &&",
    "(!has(POD.hostIPC) || POD.hostIPC == false) &&",
    "(!has(POD.shareProcessNamespace) || POD.shareProcessNamespace == false) &&",
    "size(POD.containers) == 1 && size(POD.initContainers) == 1 &&",
    "POD.containers[0].name == 'storage-reconciler' &&",
    "POD.initContainers[0].name == 'verify-effective-egress' &&",
    "POD.containers[0].image == '${local.current_release.image_repository}@${local.current_release.image_digest}' &&",
    "POD.initContainers[0].image == '${local.current_release.image_repository}@${local.current_release.image_digest}' &&",
    "POD.containers[0].args == ['storage-reconciler'] && (!has(POD.containers[0].command) || size(POD.containers[0].command) == 0) &&",
    "POD.initContainers[0].command == ['python', '-m', 'fs2_serve.storage_egress_contract'] && POD.initContainers[0].args == ${local.expected_init_args_cel} &&",
    "POD.containers[0].env == ${local.expected_container_env_cel} &&",
    "POD.containers[0].volumeMounts == ${local.expected_container_mounts_cel} && POD.initContainers[0].volumeMounts == ${local.expected_init_mounts_cel} &&",
    "(!has(POD.containers[0].envFrom) || size(POD.containers[0].envFrom) == 0) &&",
    "POD.initContainers[0].env == ${local.expected_init_env_cel} &&",
    "(!has(POD.initContainers[0].envFrom) || size(POD.initContainers[0].envFrom) == 0) &&",
    "has(POD.securityContext) && POD.securityContext.runAsNonRoot == true && POD.securityContext.runAsUser == 65532 && POD.securityContext.runAsGroup == 65532 && POD.securityContext.fsGroup == 65532 && POD.securityContext.seccompProfile.type == 'RuntimeDefault' &&",
    "[POD.containers[0], POD.initContainers[0]].all(container, has(container.securityContext) && container.securityContext.allowPrivilegeEscalation == false && container.securityContext.readOnlyRootFilesystem == true && container.securityContext.capabilities.drop == ['ALL'] && (!has(container.securityContext.privileged) || container.securityContext.privileged == false)) &&",
    "(!has(POD.ephemeralContainers) || size(POD.ephemeralContainers) == 0) &&",
    "(!has(POD.imagePullSecrets) || POD.imagePullSecrets == ${local.expected_pull_secrets_cel}) &&",
    "size(POD.volumes) == 7 && POD.volumes.all(volume, volume.name in ['egress-contract', 'egress-trust', 'kubernetes-api', 'customer-storage-crypto', 'database-ca', 'customer-storage-resource', 'customer-storage-iam']) &&",
    "size(POD.volumes.filter(volume, has(volume.secret))) == size(${local.allowed_secret_names_cel}) &&",
    "POD.volumes.filter(volume, has(volume.secret)).all(volume, volume.secret.secretName in ${local.allowed_secret_names_cel}) &&",
    "${local.allowed_secret_names_cel}.all(secretName, POD.volumes.exists(volume, has(volume.secret) && volume.secret.secretName == secretName)) &&",
    "POD.volumes.exists(volume, volume.name == 'customer-storage-crypto' && has(volume.secret) && volume.secret.secretName == '${local.current_release.crypto_secret_name}') &&",
    "POD.volumes.exists(volume, volume.name == 'database-ca' && has(volume.secret) && volume.secret.secretName == '${local.current_release.database_secret_name}') &&",
    "POD.volumes.exists(volume, volume.name == 'customer-storage-resource' && has(volume.secret) && volume.secret.secretName == '${local.current_release.resource_credentials_secret_name}') &&",
    "POD.volumes.exists(volume, volume.name == 'customer-storage-iam' && has(volume.secret) && volume.secret.secretName == '${local.current_release.iam_credentials_secret_name}') &&",
    "POD.volumes.exists(volume, volume.name == 'egress-contract' && has(volume.configMap) && volume.configMap.name == '${local.successor_contract_names[var.current_generation]}') &&",
    "POD.volumes.exists(volume, volume.name == 'egress-trust' && has(volume.configMap) && volume.configMap.name == '${local.successor_trust_names[local.current_contract.trust_generation]}') &&",
    "POD.volumes.filter(volume, has(volume.projected)).all(volume,",
    "volume.name == 'kubernetes-api' && size(volume.projected.sources) == 2 &&",
    "volume.projected.sources.exists(source, has(source.serviceAccountToken) && source.serviceAccountToken.audience == 'kubernetes.default.svc' && source.serviceAccountToken.expirationSeconds == 600 && source.serviceAccountToken.path == 'token') &&",
    "volume.projected.sources.exists(source, has(source.configMap) && source.configMap.name == 'kube-root-ca.crt') &&",
    "volume.projected.sources.all(source, has(source.serviceAccountToken) || (has(source.configMap) && source.configMap.name == 'kube-root-ca.crt'))) &&",
    "size(POD.volumes.filter(volume, has(volume.hostPath) || has(volume.persistentVolumeClaim) || has(volume.csi))) == 0",
  ])
  deployment_pod_spec_cel = replace(
    local.workload_pod_spec_template_cel,
    "POD",
    "object.spec.template.spec",
  )
  pod_spec_cel = replace(
    local.workload_pod_spec_template_cel,
    "POD",
    "object.spec",
  )
  workload_policy_spec = {
    failurePolicy = "Fail"
    matchPolicy   = "Equivalent"
    matchConstraints = {
      resourceRules = [
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["pods", "serviceaccounts"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["deployments", "daemonsets", "statefulsets", "replicasets"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = ["batch"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["jobs", "cronjobs"]
          scope       = "Namespaced"
        },
      ]
    }
    matchConditions = [{
      name = "customer-storage-successor-workload"
      expression = join(" ", [
        "request.namespace == '${local.namespace}' &&",
        "(request.userInfo.username == '${var.non_owner_identities[var.release_identity_name].username}' ||",
        "request.name == '${local.current_release_name}' ||",
        "(request.operation == 'UPDATE' ?",
        "(has(oldObject.metadata.labels) && oldObject.metadata.labels.exists(key, value, key == 'fs2.nebius.ai/storage-egress-generation' && value == '${var.current_generation}') && oldObject.metadata.labels.exists(key, value, key == 'fs2.nebius.ai/storage-rollout-generation' && value == '${var.current_release_generation}')) :",
        "(has(object.metadata.labels) && object.metadata.labels.exists(key, value, key == 'fs2.nebius.ai/storage-egress-generation' && value == '${var.current_generation}') && object.metadata.labels.exists(key, value, key == 'fs2.nebius.ai/storage-rollout-generation' && value == '${var.current_release_generation}'))))",
      ])
    }]
    validations = [
      {
        expression = "request.resource.resource in ['pods', 'serviceaccounts', 'deployments', 'replicasets']"
        message    = "Customer-storage release authority cannot create Job, CronJob, DaemonSet or StatefulSet workloads."
        reason     = "Forbidden"
      },
      {
        expression = join(" ", [
          "request.resource.resource in ['pods', 'replicasets'] ||",
          "(request.operation == 'CREATE' && request.userInfo.username == '${var.non_owner_identities[var.release_identity_name].username}')",
        ])
        message = "Only the exact release identity may create the generation-named ServiceAccount or Deployment."
        reason  = "Forbidden"
      },
      {
        expression = join(" ", [
          "request.resource.resource != 'serviceaccounts' ||",
          "(request.operation == 'CREATE' && object.metadata.name == '${local.current_release_name}' &&",
          "has(object.automountServiceAccountToken) && object.automountServiceAccountToken == false &&",
          "(!has(object.secrets) || size(object.secrets) == 0) &&",
          "(!has(object.imagePullSecrets) || size(object.imagePullSecrets) == 0))",
        ])
        message = "The release ServiceAccount must be token-blind and exact."
        reason  = "Forbidden"
      },
      {
        expression = join(" ", [
          "request.resource.resource != 'deployments' ||",
          "(request.operation == 'CREATE' && object.metadata.name == '${local.current_release_name}' &&",
          "object.metadata.labels == ${local.v3_pod_labels_cel} &&",
          "object.spec.replicas == 1 && object.spec.selector.matchLabels == ${local.v3_pod_labels_cel} &&",
          "object.spec.template.metadata.labels == ${local.v3_pod_labels_cel} &&",
          "(${local.deployment_pod_spec_cel}))",
        ])
        message = "The release Deployment differs from the signed generation, Secret allowlist, or protected scheduling contract."
        reason  = "Forbidden"
      },
      {
        expression = join(" ", [
          "request.resource.resource != 'pods' ||",
          "(request.userInfo.username == '${var.replicaset_controller_username}' &&",
          "size(object.metadata.ownerReferences) == 1 && object.metadata.ownerReferences[0].apiVersion == 'apps/v1' && object.metadata.ownerReferences[0].kind == 'ReplicaSet' &&",
          "object.metadata.ownerReferences[0].name.startsWith('${local.current_release_name}-') && object.metadata.ownerReferences[0].controller == true && object.metadata.ownerReferences[0].blockOwnerDeletion == true &&",
          "object.metadata.labels.all(key, value, (key in ${local.v3_pod_labels_cel} && ${local.v3_pod_labels_cel}[key] == value) || key == 'pod-template-hash') &&",
          "'pod-template-hash' in object.metadata.labels && object.metadata.labels['pod-template-hash'] != '' &&",
          "${local.v3_pod_labels_cel}.all(key, value, key in object.metadata.labels && object.metadata.labels[key] == value) &&",
          "(${local.pod_spec_cel}))",
        ])
        message = "A successor Pod differs from the signed generation, Secret allowlist, or protected scheduling contract."
        reason  = "Forbidden"
      },
      {
        expression = join(" ", [
          "request.resource.resource != 'replicasets' ||",
          "(request.userInfo.username == '${var.deployment_controller_username}' &&",
          "request.operation == 'CREATE' && object.metadata.name.startsWith('${local.current_release_name}-') &&",
          "size(object.metadata.ownerReferences) == 1 && object.metadata.ownerReferences[0].apiVersion == 'apps/v1' && object.metadata.ownerReferences[0].kind == 'Deployment' &&",
          "object.metadata.ownerReferences[0].name == '${local.current_release_name}' && object.metadata.ownerReferences[0].controller == true && object.metadata.ownerReferences[0].blockOwnerDeletion == true &&",
          "object.metadata.labels.all(key, value, (key in ${local.v3_pod_labels_cel} && ${local.v3_pod_labels_cel}[key] == value) || key == 'pod-template-hash') &&",
          "'pod-template-hash' in object.metadata.labels && object.metadata.labels['pod-template-hash'] != '' &&",
          "${local.v3_pod_labels_cel}.all(key, value, key in object.metadata.labels && object.metadata.labels[key] == value) &&",
          "object.spec.replicas == 1 && object.spec.selector.matchLabels == object.metadata.labels &&",
          "object.spec.template.metadata.labels == object.metadata.labels && (${local.deployment_pod_spec_cel}))",
        ])
        message = "Only the approved Deployment controller may create the exact successor ReplicaSet child."
        reason  = "Forbidden"
      },
    ]
  }
  workload_policy_sha256 = sha256(jsonencode(local.workload_policy_spec))
  release_values = {
    for generation, release in var.release_generations : generation => {
      image = {
        repository  = release.image_repository
        digest      = release.image_digest
        pullPolicy  = "IfNotPresent"
        pullSecrets = release.image_pull_secrets
      }
      authority = {
        schema                         = var.provider_authority.schema
        generation                     = var.provider_authority.generation
        manifestSha256                 = var.provider_authority.authority_manifest_sha256
        priorHeadReceiptSha256         = var.provider_authority.prior_head_receipt_sha256
        iamInventoryReceiptSha256      = var.provider_authority.provider_project_iam_inventory_receipt_sha256
        rbacInventoryReceiptSha256     = var.provider_authority.kubernetes_rbac_inventory_receipt_sha256
        predecessorCompatibilitySha256 = var.provider_authority.predecessor_compatibility_sha256
        boundaryPolicySha256           = var.provider_authority.boundary_policy_sha256
        workloadPolicySha256           = var.provider_authority.workload_policy_sha256
        releaseValuesSha256            = var.provider_authority.release_values_sha256
        providerIdentitySha256         = var.provider_authority.provider_identity_sha256
        securityGroupId                = var.provider_authority.security_group_id
        nodeGroupId                    = var.provider_authority.node_group_id
        nodeSelectorKey                = var.provider_authority.node_selector_key
        nodeSelectorValue              = var.provider_authority.node_selector_value
        taintKey                       = var.provider_authority.taint_key
        taintValue                     = var.provider_authority.taint_value
        taintEffect                    = var.provider_authority.taint_effect
      }
      contract = {
        generation         = var.current_generation
        sha256             = data.external.current_contract.result.contract_sha256
        configMapName      = local.successor_contract_names[var.current_generation]
        trustConfigMapName = local.successor_trust_names[local.current_contract.trust_generation]
        networkPolicyName  = local.successor_network_policy_names[var.current_generation]
        kubernetesApiCidrs = sort(tolist(local.current_contract.kubernetes_api_cidrs))
      }
      rollout = { generation = generation }
      predecessor = {
        schema               = "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1"
        receiptSha256        = local.predecessor_compatibility_sha256
        deploymentUid        = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
        deploymentSpecSha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_deployment.object.spec))
        networkPolicyUid     = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
        networkPolicySha256  = sha256(jsonencode(data.kubernetes_resource.predecessor_network_policy.object.spec))
        contractUid          = data.kubernetes_resource.predecessor_contract.object.metadata.uid
        contractDataSha256   = sha256(jsonencode(data.kubernetes_resource.predecessor_contract.object.data))
        policyUid            = data.kubernetes_resource.predecessor_policy.object.metadata.uid
        policySpecSha256     = sha256(jsonencode(data.kubernetes_resource.predecessor_policy.object.spec))
        bindingUid           = data.kubernetes_resource.predecessor_binding.object.metadata.uid
        bindingSpecSha256    = sha256(jsonencode(data.kubernetes_resource.predecessor_binding.object.spec))
      }
      custody = {
        acceptedSai10Commit            = var.provider_authority.accepted_sai10_commit
        acceptedSai10Tree              = var.provider_authority.accepted_sai10_tree
        independentReviewReceiptSha256 = var.provider_authority.sai10_independent_review_receipt_sha256
      }
      storage = {
        projectId                     = release.storage_project_id
        region                        = release.storage_region
        defaultMode                   = "user"
        quotaBytes                    = release.quota_bytes
        excludedTenants               = sort(tolist(release.excluded_tenants))
        resourceCredentialsSecretName = release.resource_credentials_secret_name
        iamCredentialsSecretName      = release.iam_credentials_secret_name
        databaseSecretName            = release.database_secret_name
        cryptoSecretName              = release.crypto_secret_name
        storageGeneration             = release.storage_generation
        keyTtlDays                    = release.key_ttl_days
        rotationWindowDays            = release.rotation_window_days
        actionTimeoutSeconds          = release.action_timeout_seconds
      }
      resources = {
        requests = { cpu = "50m", memory = "128Mi" }
        limits   = { cpu = "500m", memory = "512Mi" }
      }
    }
  }

  # The externally signed provider-authority ledger commits this digest. The
  # Kubernetes root therefore cannot substitute a different predecessor or
  # alter the fixed selector compatibility rule during an additive handoff.
  predecessor_compatibility = {
    schema            = "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1"
    namespace         = local.namespace
    predecessor_label = "storage-reconciler"
    successor_label   = "storage-reconciler-v2"
    deployment = {
      name        = data.kubernetes_resource.predecessor_deployment.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_deployment.object.spec))
    }
    network_policy = {
      name        = data.kubernetes_resource.predecessor_network_policy.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_network_policy.object.spec))
    }
    contract = {
      name        = data.kubernetes_resource.predecessor_contract.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_contract.object.metadata.uid
      data_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_contract.object.data))
    }
    policy = {
      name        = data.kubernetes_resource.predecessor_policy.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_policy.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_policy.object.spec))
    }
    binding = {
      name        = data.kubernetes_resource.predecessor_binding.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_binding.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_binding.object.spec))
    }
  }
  predecessor_compatibility_sha256 = sha256(jsonencode(local.predecessor_compatibility))
}

data "external" "current_contract" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/../../stages/workloads/scripts/customer_storage_egress_contract.py",
    "--terraform-external",
  ]
  query = {
    contract_json  = local.current_contract.contract_json
    public_key_pem = local.current_trust.public_key_pem
  }
}

data "external" "identity_separation" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_owner_identity.py",
    "--terraform-external",
  ]
  query = {
    security_owner_group = var.security_owner_group
    identity_inventory_json = jsonencode(merge(
      {
        owner = {
          kubeconfig_path       = var.security_owner_kubeconfig_path
          kube_context          = var.security_owner_kube_context
          username              = var.security_owner_username
          groups                = var.security_owner_groups
          category              = "owner"
          credential_sha256     = var.security_owner_credential_sha256
          provider_principal_id = var.security_owner_provider_principal_id
        }
        workloads = {
          kubeconfig_path       = var.workloads_kubeconfig_path
          kube_context          = var.workloads_kube_context
          username              = var.workloads_username
          groups                = var.workloads_groups
          category              = "workloads"
          credential_sha256     = var.workloads_credential_sha256
          provider_principal_id = var.workloads_provider_principal_id
        }
      },
      var.non_owner_identities,
    ))
    service_account_inventory_json = jsonencode(var.kubernetes_service_account_inventory)
    system_subject_inventory_json  = jsonencode(var.kubernetes_system_subject_inventory)
    protected_names_json = jsonencode({
      boundary_policy  = local.successor_boundary_policy_names[var.current_boundary_generation]
      workload_policy  = local.successor_workload_policy_names[var.current_workload_policy_generation]
      contract         = local.successor_contract_names[var.current_generation]
      trust            = local.successor_trust_names[local.current_contract.trust_generation]
      network_policy   = local.successor_network_policy_names[var.current_generation]
      release_role     = local.release_names[var.current_release_generation]
      release_workload = local.release_names[var.current_release_generation]
      namespace        = local.namespace
    })
    expected_rbac_inventory_sha256      = var.provider_authority.kubernetes_rbac_inventory_sha256
    expected_effective_authority_sha256 = var.provider_authority.kubernetes_rbac_effective_authority_sha256
  }
}

data "external" "integration_dependencies" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/../verify_sai08_integration_dependencies.py",
  ]
  query = {
    dependency_record_path = "${path.module}/../sai-08-integration-dependencies.json"
    expected_dependencies_json = jsonencode({
      sai_10_accepted_commit                            = var.provider_authority.accepted_sai10_commit
      sai_10_accepted_tree                              = var.provider_authority.accepted_sai10_tree
      sai_10_independent_review_receipt_sha256          = var.provider_authority.sai10_independent_review_receipt_sha256
      provider_authority_manifest_sha256                = var.provider_authority.authority_manifest_sha256
      provider_authority_prior_head_receipt_sha256      = var.provider_authority.prior_head_receipt_sha256
      provider_project_iam_inventory_receipt_sha256     = var.provider_authority.provider_project_iam_inventory_receipt_sha256
      provider_effective_authority_graph_receipt_sha256 = var.provider_authority.provider_effective_authority_graph_receipt_sha256
      provider_authority_adapter_sha256                 = var.provider_authority.provider_authority_adapter_sha256
      provider_state_custody_sha256                     = var.provider_authority.provider_state_custody_sha256
      boundary_state_custody_sha256                     = var.provider_authority.boundary_state_custody_sha256
      kubernetes_rbac_inventory_receipt_sha256          = var.provider_authority.kubernetes_rbac_inventory_receipt_sha256
      kubernetes_rbac_effective_authority_sha256        = var.provider_authority.kubernetes_rbac_effective_authority_sha256
      kubernetes_service_account_inventory_sha256       = var.provider_authority.kubernetes_service_account_inventory_sha256
      kubernetes_system_subject_inventory_sha256        = var.provider_authority.kubernetes_system_subject_inventory_sha256
      workload_policy_sha256                            = var.provider_authority.workload_policy_sha256
      predecessor_state_custody_sha256                  = var.provider_authority.predecessor_state_custody_sha256
      live_predecessor_compatibility_handoff_sha256     = local.predecessor_compatibility_sha256
    })
  }
}

data "external" "backend_custody" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_backend_custody.py",
  ]
  query = { module_path = path.module }
}

# Read the fixed predecessor objects exactly as compatibility inputs. They are
# never imported, changed or deleted by this root. Their UIDs and content
# digests make the additive handoff prove which live boundary remains active.
data "kubernetes_resource" "predecessor_network_policy" {
  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  metadata {
    name      = "fs2-serve-control-plane-storage-reconciler"
    namespace = local.namespace
  }
}

data "kubernetes_resource" "predecessor_contract" {
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = "fs2-customer-storage-egress-contract"
    namespace = local.namespace
  }
}

data "kubernetes_resource" "predecessor_policy" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata { name = "fs2-customer-storage-egress" }
}

data "kubernetes_resource" "predecessor_binding" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata { name = "fs2-customer-storage-egress" }
}

data "kubernetes_resource" "predecessor_deployment" {
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "fs2-serve-control-plane-storage-reconciler"
    namespace = local.namespace
  }
}

resource "terraform_data" "separate_security_owner" {
  input = {
    current_generation                 = var.current_generation
    current_boundary_generation        = var.current_boundary_generation
    current_workload_policy_generation = var.current_workload_policy_generation
    current_release_generation         = var.current_release_generation
    current_contract_sha256            = data.external.current_contract.result.contract_sha256
    security_owner_group               = var.security_owner_group
    security_owner_subject             = data.external.identity_separation.result.security_owner_subject_sha256
    workloads_subject                  = data.external.identity_separation.result.workloads_subject_sha256
    identity_inventory                 = data.external.identity_separation.result.identity_inventory_sha256
    rbac_inventory                     = data.external.identity_separation.result.rbac_inventory_sha256
    provider_authority_generation      = var.provider_authority.generation
    provider_authority_manifest        = var.provider_authority.authority_manifest_sha256
    integration_dependency_record      = data.external.integration_dependencies.result.dependency_record_sha256
    predecessor_state_custody          = var.provider_authority.predecessor_state_custody_sha256
    predecessor_compatibility          = local.predecessor_compatibility_sha256
    provider_security_group_id         = var.provider_authority.security_group_id
    provider_node_group_id             = var.provider_authority.node_group_id
    predecessor_network_policy_uid     = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
    predecessor_contract_uid           = data.kubernetes_resource.predecessor_contract.object.metadata.uid
    predecessor_policy_uid             = data.kubernetes_resource.predecessor_policy.object.metadata.uid
    predecessor_binding_uid            = data.kubernetes_resource.predecessor_binding.object.metadata.uid
    predecessor_deployment_uid         = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
    boundary_backend_config            = data.external.backend_custody.result.backend_config_sha256
    boundary_backend_lineage           = data.external.backend_custody.result.backend_lineage
    boundary_state_lineage             = data.external.backend_custody.result.state_lineage
    boundary_state_serial              = data.external.backend_custody.result.state_serial
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
    precondition {
      condition     = data.external.identity_separation.result.authorized == "true"
      error_message = "Live identity preflight did not prove an external owner and non-mutating workloads authority."
    }
    precondition {
      condition     = data.external.integration_dependencies.result.authorized == "true"
      error_message = "SAI-08 integration dependencies are not externally bound to accepted source and custody."
    }
    precondition {
      condition     = data.external.backend_custody.result.authorized == "true"
      error_message = "The initialized Terraform backend is not the separately anchored workloads-state lineage."
    }
    precondition {
      condition = (
        data.external.identity_separation.result.identity_inventory_sha256 == var.provider_authority.kubernetes_identity_inventory_sha256 &&
        sha256(jsonencode(var.kubernetes_service_account_inventory)) == var.provider_authority.kubernetes_service_account_inventory_sha256 &&
        sha256(jsonencode(var.kubernetes_system_subject_inventory)) == var.provider_authority.kubernetes_system_subject_inventory_sha256 &&
        data.external.identity_separation.result.rbac_inventory_sha256 == var.provider_authority.kubernetes_rbac_inventory_sha256 &&
        data.external.identity_separation.result.rbac_effective_authority_sha256 == var.provider_authority.kubernetes_rbac_effective_authority_sha256 &&
        sha256(var.security_owner_provider_principal_id) == var.provider_authority.authority_service_account_sha256 &&
        sha256(var.workloads_provider_principal_id) == var.provider_authority.workloads_service_account_sha256
      )
      error_message = "Kubernetes owner, workloads, release, human, break-glass or other identity inventory differs from the root-owned provider registry."
    }
    precondition {
      condition = alltrue([
        for controller in [var.deployment_controller_username, var.replicaset_controller_username] :
        contains([
          for subject in var.kubernetes_system_subject_inventory : subject.name
          if subject.kind == "User"
        ], controller)
      ])
      error_message = "Deployment and ReplicaSet controller usernames must be exact signed effective-authority subjects."
    }
    precondition {
      condition     = contains(keys(var.contract_generations), var.current_generation)
      error_message = "current_generation must be retained in contract_generations."
    }
    precondition {
      condition     = contains(keys(var.successor_boundary_generations), var.current_boundary_generation)
      error_message = "current_boundary_generation must be retained in boundary_generations."
    }
    precondition {
      condition = (
        local.boundary_policy_sha256 == var.provider_authority.boundary_policy_sha256 &&
        var.successor_boundary_generations[var.current_boundary_generation].contract_generation == var.current_generation &&
        jsondecode(var.successor_boundary_generations[var.current_boundary_generation].policy_spec_json) == local.boundary_policy_spec &&
        var.successor_boundary_generations[var.current_boundary_generation].policy_sha256 == local.boundary_policy_sha256 &&
        endswith(var.current_boundary_generation, substr(local.boundary_policy_sha256, 0, 12))
      )
      error_message = "The current admission generation is not content-bound to the externally signed policy spec."
    }
    precondition {
      condition = (
        local.workload_policy_sha256 == var.provider_authority.workload_policy_sha256 &&
        var.successor_workload_policy_generations[var.current_workload_policy_generation].contract_generation == var.current_generation &&
        var.successor_workload_policy_generations[var.current_workload_policy_generation].release_generation == var.current_release_generation &&
        jsondecode(var.successor_workload_policy_generations[var.current_workload_policy_generation].policy_spec_json) == local.workload_policy_spec &&
        var.successor_workload_policy_generations[var.current_workload_policy_generation].policy_sha256 == local.workload_policy_sha256
      )
      error_message = "The exhaustive workload admission contract differs from the externally signed provider generation."
    }
    precondition {
      condition = (
        contains(keys(var.successor_workload_policy_generations), var.current_workload_policy_generation) &&
        endswith(var.current_workload_policy_generation, substr(local.workload_policy_sha256, 0, 12))
      )
      error_message = "The current workload admission generation is not append-only and content-bound."
    }
    precondition {
      condition     = contains(keys(var.release_generations), var.current_release_generation)
      error_message = "current_release_generation must be retained in release_generations."
    }
    precondition {
      condition     = sha256(jsonencode(local.release_payloads[var.current_release_generation])) == var.provider_authority.release_values_sha256
      error_message = "The current v2 image/storage release values differ from the externally signed provider generation."
    }
    precondition {
      condition = alltrue([
        for contract in values(var.contract_generations) :
        contains(keys(var.trust_generations), contract.trust_generation)
      ])
      error_message = "Every contract generation must retain its exact trust generation."
    }
    precondition {
      condition     = data.external.current_contract.result.contract_sha256 == local.contract_digests[var.current_generation]
      error_message = "The current signed-contract verifier and immutable object digest differ."
    }
    precondition {
      condition = (
        var.provider_authority.contract_sha256 == data.external.current_contract.result.contract_sha256 &&
        var.provider_authority.provider_api_cidrs == jsondecode(data.external.current_contract.result.cidrs_json) &&
        var.provider_authority.kubernetes_api_cidrs == sort(tolist(local.current_contract.kubernetes_api_cidrs))
      )
      error_message = "The Kubernetes defense-in-depth generation must equal the external provider route contract."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.predecessor_binding.object.spec.policyName, "") == "fs2-customer-storage-egress" &&
        try(data.kubernetes_resource.predecessor_binding.object.spec.validationActions, []) == ["Deny"] &&
        try(data.kubernetes_resource.predecessor_network_policy.object.spec.podSelector.matchLabels["app.kubernetes.io/component"], "") == "storage-reconciler" &&
        try(data.kubernetes_resource.predecessor_deployment.object.spec.selector.matchLabels["app.kubernetes.io/component"], "") == "storage-reconciler"
      )
      error_message = "The fixed predecessor boundary is absent or is not the exact compatibility selector."
    }
    precondition {
      condition     = local.predecessor_compatibility_sha256 == var.provider_authority.predecessor_compatibility_sha256
      error_message = "The retained predecessor objects differ from the compatibility digest in the externally signed provider authority ledger."
    }
    precondition {
      condition     = local.predecessor_compatibility_sha256 == var.provider_authority.predecessor_state_compatibility_sha256
      error_message = "The separately anchored workloads state lineage does not custody these exact predecessor UIDs/specs."
    }
  }
}

# Freeze the predecessor singleton and add one retained custody address per v3
# policy generation. A new contract therefore adds a gate instead of updating
# the state object that authorized an earlier generation.
resource "terraform_data" "security_generation_v4" {
  for_each = var.successor_boundary_generations

  input = {
    generation                                 = each.key
    contract_generation                        = each.value.contract_generation
    boundary_policy_sha256                     = each.value.policy_sha256
    boundary_policy_spec_sha256                = sha256(jsonencode(jsondecode(each.value.policy_spec_json)))
    authority_manifest_sha256                  = var.provider_authority.authority_manifest_sha256
    prior_head_receipt_sha256                  = var.provider_authority.prior_head_receipt_sha256
    integration_dependency_record_sha256       = data.external.integration_dependencies.result.dependency_record_sha256
    identity_inventory_sha256                  = data.external.identity_separation.result.identity_inventory_sha256
    kubernetes_rbac_inventory_sha256           = data.external.identity_separation.result.rbac_inventory_sha256
    kubernetes_rbac_effective_authority_sha256 = data.external.identity_separation.result.rbac_effective_authority_sha256
    provider_authority_adapter_sha256          = var.provider_authority.provider_authority_adapter_sha256
    predecessor_compatibility_sha256           = local.predecessor_compatibility_sha256
    boundary_backend_config_sha256             = data.external.backend_custody.result.backend_config_sha256
    boundary_backend_lineage                   = data.external.backend_custody.result.backend_lineage
    boundary_state_lineage                     = data.external.backend_custody.result.state_lineage
    boundary_state_serial                      = data.external.backend_custody.result.state_serial
    boundary_state_version_id                  = data.external.backend_custody.result.state_version_id
    boundary_state_snapshot_sha256             = data.external.backend_custody.result.state_snapshot_sha256
    boundary_state_managed_addresses_sha256    = data.external.backend_custody.result.managed_addresses_sha256
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.separate_security_owner]
}

# The policies are themselves append-only. Every installed generation denies
# deletion of every protected generation and requires the dedicated external
# owner group for additions or changes. A workloads/Helm identity cannot remove
# the binding first, weaken the policy, or replace the selected NetworkPolicy.
resource "kubernetes_manifest" "boundary_policy" {
  for_each = local.boundary_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
      annotations = {
        "fs2.nebius.ai/security-owner-subject-sha256" = data.external.identity_separation.result.security_owner_subject_sha256
        "fs2.nebius.ai/workloads-subject-sha256"      = data.external.identity_separation.result.workloads_subject_sha256
      }
    }
    spec = local.boundary_policy_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.separate_security_owner]
}

resource "kubernetes_manifest" "boundary_binding" {
  for_each = local.boundary_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
    }
    spec = {
      policyName        = each.value
      validationActions = ["Deny"]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [kubernetes_manifest.boundary_policy]
}

# This companion policy constrains every workload-producing kind that the
# release identity or a delegated controller could use. Jobs, CronJobs,
# DaemonSets, StatefulSets and ReplicaSets are denied; the single Deployment,
# ServiceAccount and its generated Pods must retain the exact Secret allowlist,
# immutable generation labels, image digest and provider-enforced node target.
resource "kubernetes_manifest" "workload_policy" {
  for_each = local.workload_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
      annotations = {
        "fs2.nebius.ai/workload-policy-sha256" = local.workload_policy_sha256
      }
    }
    spec = local.workload_policy_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [kubernetes_manifest.boundary_binding]
}

resource "kubernetes_manifest" "workload_binding" {
  for_each = local.workload_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
    }
    spec = {
      policyName        = each.value
      validationActions = ["Deny"]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [kubernetes_manifest.workload_policy]
}

# Compatibility-v3 objects deliberately use a disjoint name and selector
# space. Each retained policy contains the exact spec for only its own signed
# generation, so it protects broad selectors that include that generation but
# cannot reject a later exact generation during additive overlap.
resource "kubernetes_manifest" "boundary_policy_v3" {
  for_each = var.successor_boundary_generations

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.successor_boundary_policy_names[each.key]
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
        "fs2.nebius.ai/compatibility-epoch" = "v3"
      }
      annotations = {
        "fs2.nebius.ai/boundary-policy-sha256"        = each.value.policy_sha256
        "fs2.nebius.ai/security-owner-subject-sha256" = data.external.identity_separation.result.security_owner_subject_sha256
        "fs2.nebius.ai/workloads-subject-sha256"      = data.external.identity_separation.result.workloads_subject_sha256
      }
    }
    spec = jsondecode(each.value.policy_spec_json)
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner-v3"
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [terraform_data.security_generation_v4]
}

resource "kubernetes_manifest" "boundary_binding_v3" {
  for_each = var.successor_boundary_generations
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.successor_boundary_policy_names[each.key]
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
        "fs2.nebius.ai/compatibility-epoch" = "v3"
      }
    }
    spec = {
      policyName        = local.successor_boundary_policy_names[each.key]
      validationActions = ["Deny"]
    }
  }
  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner-v3"
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [kubernetes_manifest.boundary_policy_v3]
}

resource "kubernetes_manifest" "workload_policy_v3" {
  for_each = var.successor_workload_policy_generations
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.successor_workload_policy_names[each.key]
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
        "fs2.nebius.ai/compatibility-epoch" = "v3"
      }
      annotations = {
        "fs2.nebius.ai/workload-policy-sha256" = each.value.policy_sha256
      }
    }
    spec = jsondecode(each.value.policy_spec_json)
  }
  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner-v3"
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [kubernetes_manifest.boundary_binding_v3]
}

resource "kubernetes_manifest" "workload_binding_v3" {
  for_each = var.successor_workload_policy_generations
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.successor_workload_policy_names[each.key]
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
        "fs2.nebius.ai/compatibility-epoch" = "v3"
      }
    }
    spec = {
      policyName        = local.successor_workload_policy_names[each.key]
      validationActions = ["Deny"]
    }
  }
  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner-v3"
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [kubernetes_manifest.workload_policy_v3]
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = local.legacy_trusts

  metadata {
    name      = local.trust_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/security-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/public-key-sha256" = sha256(each.value.public_key_pem)
    }
  }
  immutable = true
  data = {
    "public-key.pem" = each.value.public_key_pem
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.workload_binding]
}

resource "kubernetes_config_map_v1" "contract" {
  for_each = local.legacy_contracts

  metadata {
    name      = local.contract_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/security-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256"  = local.contract_digests[each.key]
      "fs2.nebius.ai/storage-egress-valid-until"      = jsondecode(each.value.contract_json).valid_until
      "fs2.nebius.ai/storage-egress-trust-generation" = each.value.trust_generation
    }
  }
  immutable = true
  data = {
    "contract.json"             = each.value.contract_json
    "kubernetes-api-cidrs.json" = jsonencode(sort(tolist(each.value.kubernetes_api_cidrs)))
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_manifest.workload_binding,
    kubernetes_config_map_v1.trust,
  ]
}

resource "kubernetes_network_policy_v1" "contract" {
  for_each = local.legacy_contracts

  metadata {
    name      = local.network_policy_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"            = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"            = "customer-storage-egress"
      "fs2.nebius.ai/security-generation"       = each.key
      "fs2.nebius.ai/storage-egress-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256" = local.contract_digests[each.key]
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name"                  = "fs2-serve-control-plane"
        "app.kubernetes.io/instance"              = "fs2-serve-control-plane"
        "app.kubernetes.io/component"             = "storage-reconciler-v2"
        "fs2.nebius.ai/storage-egress-generation" = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "coredns"
            "app.kubernetes.io/name"     = "coredns"
            "k8s-app"                    = "coredns"
          }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-data" }
        }
        pod_selector {
          match_labels = { "cnpg.io/cluster" = "fs2-control-db" }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    egress {
      dynamic "to" {
        for_each = toset(jsondecode(each.value.contract_json).cidrs)
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }

    egress {
      dynamic "to" {
        for_each = each.value.kubernetes_api_cidrs
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_config_map_v1.contract,
  ]
}

resource "kubernetes_config_map_v1" "trust_v3" {
  for_each = local.successor_trusts
  metadata {
    name      = local.successor_trust_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/security-generation" = each.key
      "fs2.nebius.ai/compatibility-epoch" = "v3"
    }
    annotations = { "fs2.nebius.ai/public-key-sha256" = sha256(each.value.public_key_pem) }
  }
  immutable = true
  data      = { "public-key.pem" = each.value.public_key_pem }
  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.workload_binding_v3]
}

resource "kubernetes_config_map_v1" "contract_v3" {
  for_each = local.successor_contracts
  metadata {
    name      = local.successor_contract_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"            = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"            = "customer-storage-egress"
      "fs2.nebius.ai/security-generation"       = each.key
      "fs2.nebius.ai/storage-egress-generation" = each.key
      "fs2.nebius.ai/compatibility-epoch"       = "v3"
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256"  = local.contract_digests[each.key]
      "fs2.nebius.ai/storage-egress-valid-until"      = jsondecode(each.value.contract_json).valid_until
      "fs2.nebius.ai/storage-egress-trust-generation" = each.value.trust_generation
    }
  }
  immutable = true
  data = {
    "contract.json"             = each.value.contract_json
    "kubernetes-api-cidrs.json" = jsonencode(sort(tolist(each.value.kubernetes_api_cidrs)))
  }
  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.workload_binding_v3, kubernetes_config_map_v1.trust_v3]
}

resource "kubernetes_network_policy_v1" "contract_v3" {
  for_each = local.successor_contracts
  metadata {
    name      = local.successor_network_policy_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"            = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"            = "customer-storage-egress"
      "fs2.nebius.ai/security-generation"       = each.key
      "fs2.nebius.ai/storage-egress-generation" = each.key
      "fs2.nebius.ai/compatibility-epoch"       = "v3"
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256" = local.contract_digests[each.key]
    }
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name"                  = "fs2-serve-control-plane"
        "app.kubernetes.io/instance"              = "fs2-serve-control-plane"
        "app.kubernetes.io/component"             = "storage-reconciler-v3"
        "fs2.nebius.ai/storage-egress-generation" = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "coredns"
            "app.kubernetes.io/name"     = "coredns"
            "k8s-app"                    = "coredns"
          }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "fs2-data" } }
        pod_selector { match_labels = { "cnpg.io/cluster" = "fs2-control-db" } }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }
    egress {
      dynamic "to" {
        for_each = toset(jsondecode(each.value.contract_json).cidrs)
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
    egress {
      dynamic "to" {
        for_each = each.value.kubernetes_api_cidrs
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.boundary_binding_v3, kubernetes_config_map_v1.contract_v3]
}

# The external security owner creates the immutable, generation-named
# inventory permission before the release identity creates the ServiceAccount
# and Deployment. The release identity therefore needs neither bind nor
# escalate authority and cannot manufacture a more privileged RoleBinding.
resource "kubernetes_role_v1" "reconciler_inventory" {
  for_each = local.legacy_releases

  metadata {
    name      = local.legacy_release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"     = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"     = "customer-storage-egress"
      "fs2.nebius.ai/release-generation" = each.key
    }
  }

  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    verbs      = ["get", "list"]
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.boundary_binding]
}

resource "kubernetes_role_binding_v1" "reconciler_inventory" {
  for_each = local.legacy_releases

  metadata {
    name      = local.legacy_release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"     = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"     = "customer-storage-egress"
      "fs2.nebius.ai/release-generation" = each.key
    }
  }

  subject {
    kind      = "ServiceAccount"
    name      = local.legacy_release_names[each.key]
    namespace = local.namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.reconciler_inventory[each.key].metadata[0].name
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_role_v1.reconciler_inventory]
}

resource "kubernetes_role_v1" "reconciler_inventory_v3" {
  for_each = local.successor_releases
  metadata {
    name      = local.release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/release-generation"  = each.key
      "fs2.nebius.ai/compatibility-epoch" = "v3"
    }
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    verbs      = ["get", "list"]
  }
  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.boundary_binding_v3]
}

resource "kubernetes_role_binding_v1" "reconciler_inventory_v3" {
  for_each = local.successor_releases
  metadata {
    name      = local.release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/release-generation"  = each.key
      "fs2.nebius.ai/compatibility-epoch" = "v3"
    }
  }
  subject {
    kind      = "ServiceAccount"
    name      = local.release_names[each.key]
    namespace = local.namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.reconciler_inventory_v3[each.key].metadata[0].name
  }
  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_role_v1.reconciler_inventory_v3]
}

resource "helm_release" "storage_reconciler_v3" {
  provider = helm.storage_release
  for_each = local.successor_releases

  name             = local.release_names[each.key]
  namespace        = local.namespace
  create_namespace = false
  chart            = "${path.module}/../../charts/security/customer-storage-reconciler-v2"
  values           = [yamlencode(local.release_values[each.key])]
  atomic           = false
  cleanup_on_fail  = false
  force_update     = false
  replace          = false
  reset_values     = true
  reuse_values     = false
  wait             = true
  wait_for_jobs    = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
    precondition {
      condition = (
        var.non_owner_identities[var.release_identity_name].category == "release" &&
        each.key == each.value.rollout_generation &&
        endswith(
          each.key,
          substr(sha256(jsonencode({
            authority     = var.provider_authority.authority_manifest_sha256
            contract      = data.external.current_contract.result.contract_sha256
            custodyCommit = var.provider_authority.accepted_sai10_commit
            custodyTree   = var.provider_authority.accepted_sai10_tree
            custodyReview = var.provider_authority.sai10_independent_review_receipt_sha256
            image         = each.value.image_digest
            releaseValues = var.provider_authority.release_values_sha256
          })), 0, 12),
        )
      )
      error_message = "The v3 release identity or content-bound rollout generation differs from the signed authority."
    }
  }
  depends_on = [
    kubernetes_manifest.boundary_binding_v3,
    kubernetes_manifest.workload_binding_v3,
    kubernetes_config_map_v1.trust_v3,
    kubernetes_config_map_v1.contract_v3,
    kubernetes_network_policy_v1.contract_v3,
    kubernetes_role_binding_v1.reconciler_inventory_v3,
  ]
}

# The chart is part of the canonical additive release path rather than an
# operator-side command. Each map key creates one content-named Helm release;
# retained entries are never upgraded, rolled back, uninstalled, or removed.
# HELM_DRIVER=configmap is mandatory for this root so the narrow release
# identity has no Secret read/write permission.
resource "helm_release" "storage_reconciler_v2" {
  provider = helm.storage_release
  for_each = local.legacy_releases

  name             = local.legacy_release_names[each.key]
  namespace        = local.namespace
  create_namespace = false
  chart            = "${path.module}/../../charts/security/customer-storage-reconciler-v2"
  values           = [yamlencode(local.release_values[each.key])]

  atomic          = false
  cleanup_on_fail = false
  force_update    = false
  replace         = false
  reset_values    = true
  reuse_values    = false
  wait            = true
  wait_for_jobs   = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_manifest.workload_binding,
    kubernetes_config_map_v1.trust,
    kubernetes_config_map_v1.contract,
    kubernetes_network_policy_v1.contract,
    kubernetes_role_binding_v1.reconciler_inventory,
  ]
}
