locals {
  namespace = "fs2-system"

  current_contract = var.contract_generations[var.current_generation]
  current_trust    = var.trust_generations[local.current_contract.trust_generation]

  contract_digests = {
    for generation, contract in var.contract_generations :
    generation => sha256(jsonencode(jsondecode(contract.contract_json)))
  }
  contract_names = {
    for generation in keys(var.contract_generations) :
    generation => "fs2-customer-storage-egress-contract-${generation}"
  }
  trust_names = {
    for generation in keys(var.trust_generations) :
    generation => "fs2-customer-storage-egress-trust-${generation}"
  }
  network_policy_names = {
    for generation in keys(var.contract_generations) :
    generation => "fs2-customer-storage-egress-${generation}"
  }
  boundary_policy_names = {
    for generation in var.boundary_generations :
    generation => "fs2-customer-storage-egress-boundary-${generation}"
  }
  v2_pod_labels = {
    "app.kubernetes.io/name"      = "fs2-serve-control-plane"
    "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
    "app.kubernetes.io/component" = "storage-reconciler-v2"
  }
  v2_pod_labels_cel = jsonencode(local.v2_pod_labels)
  v2_dynamic_label_keys_cel = jsonencode([
    "fs2.nebius.ai/storage-egress-generation",
    "fs2.nebius.ai/storage-rollout-generation",
  ])
  v2_selector_matches_object_cel = join(" ", [
    "(!has(object.spec.podSelector.matchLabels) ||",
    "object.spec.podSelector.matchLabels.all(key, value,",
    "(key in ${local.v2_pod_labels_cel} && ${local.v2_pod_labels_cel}[key] == value) ||",
    "key in ${local.v2_dynamic_label_keys_cel})) &&",
    "(!has(object.spec.podSelector.matchExpressions) ||",
    "object.spec.podSelector.matchExpressions.all(term,",
    "(term.operator == 'In' && ((term.key in ${local.v2_pod_labels_cel} && term.values.exists(value, ${local.v2_pod_labels_cel}[term.key] == value)) || (term.key in ${local.v2_dynamic_label_keys_cel} && size(term.values) > 0))) ||",
    "(term.operator == 'NotIn' && ((term.key in ${local.v2_pod_labels_cel} && !term.values.exists(value, ${local.v2_pod_labels_cel}[term.key] == value)) || term.key in ${local.v2_dynamic_label_keys_cel} || (!(term.key in ${local.v2_pod_labels_cel}) && !(term.key in ${local.v2_dynamic_label_keys_cel})))) ||",
    "(term.operator == 'Exists' && (term.key in ${local.v2_pod_labels_cel} || term.key in ${local.v2_dynamic_label_keys_cel})) ||",
    "(term.operator == 'DoesNotExist' && !(term.key in ${local.v2_pod_labels_cel}) && !(term.key in ${local.v2_dynamic_label_keys_cel}))))",
  ])
  v2_selector_matches_old_object_cel = replace(
    local.v2_selector_matches_object_cel,
    "object.spec.podSelector",
    "oldObject.spec.podSelector",
  )
  boundary_policy_spec = {
    failurePolicy = "Fail"
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
      ]
    }
    matchConditions = [{
      name = "customer-storage-security-boundary"
      expression = join(" ", [
        "(request.resource.group == 'admissionregistration.k8s.io' &&",
        "request.name.startsWith('fs2-customer-storage-egress-boundary-')) ||",
        "(request.resource.group == 'rbac.authorization.k8s.io' && request.namespace == '${local.namespace}' &&",
        "request.name.startsWith('fs2-storage-v2-')) ||",
        "(request.namespace == '${local.namespace}' &&",
        "request.name.startsWith('fs2-customer-storage-egress-')) ||",
        "(request.resource.group == 'networking.k8s.io' && request.namespace == '${local.namespace}' &&",
        "(request.operation == 'DELETE' ?",
        "(oldObject.metadata.name.startsWith('fs2-customer-storage-egress-g') || (${local.v2_selector_matches_old_object_cel})) :",
        "(object.metadata.name.startsWith('fs2-customer-storage-egress-g') || (${local.v2_selector_matches_object_cel}))))",
      ])
    }]
    validations = [
      {
        expression = "request.operation == 'CREATE'"
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
  release_names = {
    for generation in keys(var.release_generations) :
    generation => "fs2-storage-v2-${element(reverse(split("-", var.current_generation)), 0)}-${element(reverse(split("-", generation)), 0)}"
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
        releaseValuesSha256             = var.provider_authority.release_values_sha256
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
        configMapName      = local.contract_names[var.current_generation]
        trustConfigMapName = local.trust_names[local.current_contract.trust_generation]
        networkPolicyName  = local.network_policy_names[var.current_generation]
        kubernetesApiCidrs = sort(tolist(local.current_contract.kubernetes_api_cidrs))
      }
      rollout = { generation = generation }
      predecessor = {
        schema                = "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1"
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
        keyTtlDays                     = release.key_ttl_days
        rotationWindowDays             = release.rotation_window_days
        actionTimeoutSeconds            = release.action_timeout_seconds
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
          kubeconfig_path      = var.security_owner_kubeconfig_path
          kube_context         = var.security_owner_kube_context
          username             = var.security_owner_username
          category             = "owner"
          credential_sha256    = var.security_owner_credential_sha256
          provider_principal_id = var.security_owner_provider_principal_id
        }
        workloads = {
          kubeconfig_path      = var.workloads_kubeconfig_path
          kube_context         = var.workloads_kube_context
          username             = var.workloads_username
          category             = "workloads"
          credential_sha256    = var.workloads_credential_sha256
          provider_principal_id = var.workloads_provider_principal_id
        }
      },
      var.non_owner_identities,
    ))
    protected_names_json = jsonencode({
      boundary_policy = local.boundary_policy_names[var.current_boundary_generation]
      contract        = local.contract_names[var.current_generation]
      trust           = local.trust_names[local.current_contract.trust_generation]
      network_policy  = local.network_policy_names[var.current_generation]
      release_role    = local.release_names[var.current_release_generation]
      namespace       = local.namespace
    })
    expected_rbac_inventory_sha256 = var.provider_authority.kubernetes_rbac_inventory_sha256
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
      sai_10_accepted_commit                              = var.provider_authority.accepted_sai10_commit
      sai_10_accepted_tree                                = var.provider_authority.accepted_sai10_tree
      sai_10_independent_review_receipt_sha256             = var.provider_authority.sai10_independent_review_receipt_sha256
      provider_authority_manifest_sha256                   = var.provider_authority.authority_manifest_sha256
      provider_authority_prior_head_receipt_sha256         = var.provider_authority.prior_head_receipt_sha256
      provider_project_iam_inventory_receipt_sha256        = var.provider_authority.provider_project_iam_inventory_receipt_sha256
      kubernetes_rbac_inventory_receipt_sha256              = var.provider_authority.kubernetes_rbac_inventory_receipt_sha256
      predecessor_state_custody_sha256                     = var.provider_authority.predecessor_state_custody_sha256
      live_predecessor_compatibility_handoff_sha256        = local.predecessor_compatibility_sha256
    })
  }
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
    current_generation             = var.current_generation
    current_boundary_generation    = var.current_boundary_generation
    current_release_generation     = var.current_release_generation
    current_contract_sha256        = data.external.current_contract.result.contract_sha256
    security_owner_group           = var.security_owner_group
    security_owner_subject         = data.external.identity_separation.result.security_owner_subject_sha256
    workloads_subject              = data.external.identity_separation.result.workloads_subject_sha256
    identity_inventory             = data.external.identity_separation.result.identity_inventory_sha256
    rbac_inventory                 = data.external.identity_separation.result.rbac_inventory_sha256
    provider_authority_generation  = var.provider_authority.generation
    provider_authority_manifest    = var.provider_authority.authority_manifest_sha256
    integration_dependency_record  = data.external.integration_dependencies.result.dependency_record_sha256
    predecessor_state_custody      = var.provider_authority.predecessor_state_custody_sha256
    predecessor_compatibility      = local.predecessor_compatibility_sha256
    provider_security_group_id     = var.provider_authority.security_group_id
    provider_node_group_id         = var.provider_authority.node_group_id
    predecessor_network_policy_uid = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
    predecessor_contract_uid       = data.kubernetes_resource.predecessor_contract.object.metadata.uid
    predecessor_policy_uid         = data.kubernetes_resource.predecessor_policy.object.metadata.uid
    predecessor_binding_uid        = data.kubernetes_resource.predecessor_binding.object.metadata.uid
    predecessor_deployment_uid     = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
  }

  lifecycle {
    precondition {
      condition     = data.external.identity_separation.result.authorized == "true"
      error_message = "Live identity preflight did not prove an external owner and non-mutating workloads authority."
    }
    precondition {
      condition     = data.external.integration_dependencies.result.authorized == "true"
      error_message = "SAI-08 integration dependencies are not externally bound to accepted source and custody."
    }
    precondition {
      condition = (
        data.external.identity_separation.result.identity_inventory_sha256 == var.provider_authority.kubernetes_identity_inventory_sha256 &&
        data.external.identity_separation.result.rbac_inventory_sha256 == var.provider_authority.kubernetes_rbac_inventory_sha256 &&
        sha256(var.security_owner_provider_principal_id) == var.provider_authority.authority_service_account_sha256 &&
        sha256(var.workloads_provider_principal_id) == var.provider_authority.workloads_service_account_sha256
      )
      error_message = "Kubernetes owner, workloads, release, human, break-glass or other identity inventory differs from the root-owned provider registry."
    }
    precondition {
      condition     = contains(keys(var.contract_generations), var.current_generation)
      error_message = "current_generation must be retained in contract_generations."
    }
    precondition {
      condition     = contains(var.boundary_generations, var.current_boundary_generation)
      error_message = "current_boundary_generation must be retained in boundary_generations."
    }
    precondition {
      condition = (
        local.boundary_policy_sha256 == var.provider_authority.boundary_policy_sha256 &&
        endswith(var.current_boundary_generation, substr(local.boundary_policy_sha256, 0, 12))
      )
      error_message = "The current admission generation is not content-bound to the externally signed policy spec."
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
  }

  depends_on = [kubernetes_manifest.boundary_policy]
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = var.trust_generations

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

  depends_on = [kubernetes_manifest.boundary_binding]
}

resource "kubernetes_config_map_v1" "contract" {
  for_each = var.contract_generations

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
    kubernetes_config_map_v1.trust,
  ]
}

resource "kubernetes_network_policy_v1" "contract" {
  for_each = var.contract_generations

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

# The external security owner creates the immutable, generation-named
# inventory permission before the release identity creates the ServiceAccount
# and Deployment. The release identity therefore needs neither bind nor
# escalate authority and cannot manufacture a more privileged RoleBinding.
resource "kubernetes_role_v1" "reconciler_inventory" {
  for_each = var.release_generations

  metadata {
    name      = local.release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"       = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"       = "customer-storage-egress"
      "fs2.nebius.ai/release-generation"   = each.key
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
  for_each = var.release_generations

  metadata {
    name      = local.release_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"       = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"       = "customer-storage-egress"
      "fs2.nebius.ai/release-generation"   = each.key
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
    name      = kubernetes_role_v1.reconciler_inventory[each.key].metadata[0].name
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_role_v1.reconciler_inventory]
}

# The chart is part of the canonical additive release path rather than an
# operator-side command. Each map key creates one content-named Helm release;
# retained entries are never upgraded, rolled back, uninstalled, or removed.
# HELM_DRIVER=configmap is mandatory for this root so the narrow release
# identity has no Secret read/write permission.
resource "helm_release" "storage_reconciler_v2" {
  provider = helm.storage_release
  for_each = var.release_generations

  name             = local.release_names[each.key]
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
            image          = each.value.image_digest
            releaseValues  = var.provider_authority.release_values_sha256
          })), 0, 12),
        )
      )
      error_message = "The v2 release identity or content-bound rollout generation differs from the signed authority."
    }
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_config_map_v1.trust,
    kubernetes_config_map_v1.contract,
    kubernetes_network_policy_v1.contract,
    kubernetes_role_binding_v1.reconciler_inventory,
  ]
}
